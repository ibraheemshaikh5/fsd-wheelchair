// One streaming session: everything started by one tap of Start (or by the
// page becoming visible again), and undone by stop(). Browser APIs come in as
// dependencies so the start/stop sequencing can be unit tested with fakes.

import {
  encodeFrame as encodeFrameMessage,
  type GpsMessage,
  type PhoneMessage,
  PROTOCOL_VERSION,
  type ServerMessage,
} from '../shared/protocol';
import { type CaptureSize, type FrameMetadataLike, frameTimeMs } from './camera';
import { sampleFromPong } from './clock';
import { type OrientationLike, toHeadingMessage } from './heading';
import { INITIAL_LIVE, type LiveState, type MotionAccess } from './live';
import { type PositionLike, recentFix, toGpsMessage } from './location';
import { addMotion, EMPTY_BATCH, type ImuBatch, type MotionLike, takeBatch } from './motion';
import type { CaptureSettings } from './settings';
import { CLOSE_REPLACED, type TransportState } from './transport';

export type Unsubscribe = () => void;
export type CameraChange = 'muted' | 'unmuted' | 'ended';
type TrackEventName = 'mute' | 'unmute' | 'ended';

export interface TrackLike {
  stop(): void;
  addEventListener(type: TrackEventName, listener: () => void): void;
  removeEventListener(type: TrackEventName, listener: () => void): void;
}

export interface StreamLike {
  getTracks(): readonly TrackLike[];
  getVideoTracks(): readonly TrackLike[];
}

export interface CameraLike<S extends StreamLike> {
  readonly stream: S;
  readonly label: string;
}

export interface PreviewSize {
  readonly width: number;
  readonly height: number;
}

export interface EncodedFrameLike {
  readonly bytes: Uint8Array<ArrayBuffer>;
  readonly w: number;
  readonly h: number;
}

export interface TransportHandlers {
  readonly onState: (state: TransportState) => void;
  readonly onMessage: (message: ServerMessage) => void;
}

/** The parts of Transport a session uses. */
export interface TransportLike {
  start(): void;
  stop(): void;
  sendJson(message: unknown, droppable?: boolean): boolean;
  sendFrame(bytes: Uint8Array<ArrayBuffer>): boolean;
  checkLiveness(nowMs: number, timeoutMs: number): void;
}

export interface SessionDeps<S extends StreamLike> {
  /** Open the rear camera; give up (and release any stream) once `signal` aborts. */
  readonly openCamera: (size: CaptureSize, signal: AbortSignal) => Promise<CameraLike<S>>;
  /** Show the stream in the preview and play it; resolves with the video size. */
  readonly playPreview: (stream: S) => Promise<PreviewSize>;
  /** Detach the stream from the preview, if it is still the one shown. */
  readonly clearPreview: (stream: S) => void;
  readonly resumePreview: () => Promise<void>;
  readonly createTransport: (handlers: TransportHandlers) => TransportLike;
  readonly startFrameLoop: (fps: number, onFrame: (metadata: FrameMetadataLike | undefined) => void) => Unsubscribe;
  readonly encodeFrame: (maxSide: number, quality: number) => Promise<EncodedFrameLike | null>;
  readonly watchLocation: (onPosition: (position: PositionLike) => void, onError: (message: string) => void) => Unsubscribe;
  readonly keepScreenOn: (onChange: (held: boolean) => void) => Unsubscribe;
  readonly listenOrientation: (onReading: (event: OrientationLike) => void) => Unsubscribe;
  readonly listenMotion: (onMotion: (event: MotionLike) => void) => Unsubscribe;
  readonly setInterval: (fn: () => void, ms: number) => number;
  readonly clearInterval: (id: number) => void;
  /** Phone wall clock, ms since the Unix epoch. */
  readonly now: () => number;
  /** Monotonic clock (performance.now): the clock video frame callback times use. */
  readonly monotonicNow: () => number;
  readonly userAgent: string;
}

export interface SessionCallbacks {
  readonly onCamera: (change: CameraChange) => void;
  /** A newer phone connection replaced this one; the session must not reconnect. */
  readonly onTakenOver: () => void;
  readonly onError: (message: string) => void;
}

/** Thrown by start() when stop() was called before it finished. Not an error to show. */
export class SessionCancelled extends Error {
  constructor() {
    super('The session was stopped while it was starting.');
    this.name = 'SessionCancelled';
  }
}

const CAMERA_REQUEST = { width: 1280, height: 720 } as const;
const PING_INTERVAL_MS = 1000;
/** About five missed pongs: the link is dead even if the socket still looks open. */
export const LIVENESS_TIMEOUT_MS = 5000;
const HEADING_INTERVAL_MS = 100;
const IMU_FLUSH_MS = 100;
const IMU_MAX_SAMPLES = 200;
// The service ages GPS by its fix_t, so a resent fix is honest by construction.
// This only avoids resending a fix the service would already treat as stale
// (its gps_stale_ms is 3 s).
const GPS_RESEND_MAX_AGE_MS = 2000;
const TRACK_EVENTS: readonly [TrackEventName, CameraChange][] = [
  ['mute', 'muted'],
  ['unmute', 'unmuted'],
  ['ended', 'ended'],
];

const stopTracks = (stream: StreamLike): void => stream.getTracks().forEach((track) => track.stop());

export class Session<S extends StreamLike> {
  private cleanups: readonly Unsubscribe[] = [];
  private readonly transport: TransportLike;
  private readonly abort = new AbortController();
  private stream: S | null = null;
  private stopped = false;
  private state: LiveState = INITIAL_LIVE;
  private seq = 0;
  private encoding = false;
  private imu: ImuBatch = EMPTY_BATCH;
  private lastHeadingAt = 0;
  private lastFix: GpsMessage | null = null;
  private cameraInfo = { label: 'camera', width: 0, height: 0 };

  constructor(
    private readonly config: CaptureSettings,
    private readonly deps: SessionDeps<S>,
    private readonly callbacks: SessionCallbacks,
  ) {
    this.transport = deps.createTransport({
      onState: (state) => this.onConnection(state),
      onMessage: (message) => this.onServerMessage(message),
    });
  }

  get live(): LiveState {
    return this.state;
  }

  /** Rejects with SessionCancelled if stop() is called before it finishes. */
  async start(motionAccess: Promise<MotionAccess>): Promise<void> {
    const size = { ...CAMERA_REQUEST, fps: this.config.fps };
    const camera = await this.settle(this.deps.openCamera(size, this.abort.signal), (c) => stopTracks(c.stream));
    this.stream = camera.stream;
    const preview = await this.settle(this.deps.playPreview(camera.stream));
    this.watchTrack(camera.stream);
    this.cameraInfo = {
      label: camera.label,
      width: preview.width > 0 ? preview.width : CAMERA_REQUEST.width,
      height: preview.height > 0 ? preview.height : CAMERA_REQUEST.height,
    };
    this.update({ cameraLabel: camera.label });
    this.startStreaming();

    const motion = await this.settle(motionAccess);
    this.update({ motion });
    if (motion !== 'denied') {
      this.addCleanup(this.deps.listenOrientation((event) => this.onOrientation(event)));
      this.addCleanup(this.deps.listenMotion((event) => this.onMotion(event)));
    }
  }

  /** Undo everything start() did so far. Safe to call more than once. */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.abort.abort();
    const cleanups = this.cleanups;
    this.cleanups = [];
    cleanups.forEach((cleanup) => cleanup());
    this.transport.stop();
    if (this.stream !== null) {
      stopTracks(this.stream);
      this.deps.clearPreview(this.stream);
      this.stream = null;
    }
  }

  replayPreview(): Promise<void> {
    return this.deps.resumePreview();
  }

  /** Await a start step; if stop() was called meanwhile, discard its result and cancel. */
  private async settle<T>(step: Promise<T>, discard: (value: T) => void = () => {}): Promise<T> {
    let value: T;
    try {
      value = await step;
    } catch (error) {
      throw this.stopped ? new SessionCancelled() : error;
    }
    if (this.stopped) {
      discard(value);
      throw new SessionCancelled();
    }
    return value;
  }

  private addCleanup(cleanup: Unsubscribe): void {
    this.cleanups = [...this.cleanups, cleanup];
  }

  private watchTrack(stream: S): void {
    const track = stream.getVideoTracks()[0];
    if (track === undefined) return;
    for (const [type, change] of TRACK_EVENTS) {
      const listener = (): void => {
        if (!this.stopped) this.callbacks.onCamera(change);
      };
      track.addEventListener(type, listener);
      this.addCleanup(() => track.removeEventListener(type, listener));
    }
  }

  private startStreaming(): void {
    this.transport.start();
    this.addCleanup(this.deps.startFrameLoop(this.config.fps, (metadata) => this.onFrame(metadata)));
    this.addCleanup(this.deps.watchLocation((position) => this.onPosition(position), this.callbacks.onError));
    this.addCleanup(this.deps.keepScreenOn((held) => this.update({ wakeLock: held })));
    const ping = this.deps.setInterval(() => this.ping(), PING_INTERVAL_MS);
    this.addCleanup(() => this.deps.clearInterval(ping));
  }

  private update(changes: Partial<LiveState>): void {
    this.state = { ...this.state, ...changes };
  }

  private count(key: keyof LiveState['counters']): void {
    const counters = this.state.counters;
    this.update({ counters: { ...counters, [key]: counters[key] + 1 } });
  }

  private send(message: PhoneMessage, droppable = false): void {
    this.transport.sendJson(message, droppable);
  }

  private onConnection(state: TransportState): void {
    if (this.stopped) return;
    this.update({ connection: state });
    if (state.status === 'stopped' && state.lastClose?.code === CLOSE_REPLACED) {
      this.callbacks.onTakenOver();
      return;
    }
    if (state.status !== 'open') return;
    // The service forgets the phone and its clock offset when the socket drops,
    // so introduce it and resync the clock on every connect.
    this.sendHello();
    this.ping();
    const fix = recentFix(this.lastFix, this.deps.now(), GPS_RESEND_MAX_AGE_MS);
    if (fix !== null) this.send(fix);
  }

  private sendHello(): void {
    this.send({
      v: PROTOCOL_VERSION,
      type: 'hello',
      t: this.deps.now(),
      user_agent: this.deps.userAgent,
      camera: { ...this.cameraInfo, fps: this.config.fps },
      mount: { height_m: this.config.mountHeightM, pitch_deg: this.config.mountPitchDeg },
    });
  }

  private onServerMessage(message: ServerMessage): void {
    if (this.stopped) return;
    if (message.type === 'pong') {
      const sample = sampleFromPong(message, this.deps.now());
      if (sample !== null) this.update({ clock: sample });
    } else {
      this.callbacks.onError(`Service rejected a message: ${message.message}`);
    }
  }

  private ping(): void {
    const now = this.deps.now();
    this.transport.checkLiveness(now, LIVENESS_TIMEOUT_MS);
    this.send({
      v: PROTOCOL_VERSION,
      type: 'ping',
      t: now,
      rtt_ms: this.state.clock?.rtt_ms ?? null,
      offset_ms: this.state.clock?.offset_ms ?? null,
    });
  }

  private onFrame(metadata: FrameMetadataLike | undefined): void {
    if (this.stopped) return;
    const seq = this.seq;
    this.seq += 1;
    this.count('captured');
    // Latest-wins: if the last frame is still encoding, or the link is down, drop this one.
    if (this.encoding || this.state.connection.status !== 'open') {
      this.count('skipped');
      return;
    }
    const t = frameTimeMs(metadata, this.deps.now(), this.deps.monotonicNow());
    this.encoding = true;
    this.deps
      .encodeFrame(this.config.maxWidth, this.config.jpegQuality)
      .then((encoded) => {
        const sent =
          encoded !== null &&
          this.transport.sendFrame(encodeFrameMessage({ seq, t, w: encoded.w, h: encoded.h }, encoded.bytes));
        this.count(sent ? 'sent' : 'skipped');
      })
      .catch(() => this.count('skipped'))
      .finally(() => {
        this.encoding = false;
      });
  }

  private onPosition(position: PositionLike): void {
    if (this.stopped) return;
    const message = toGpsMessage(position, this.deps.now());
    this.lastFix = message;
    this.update({ gpsAccuracyM: message.accuracy_m });
    this.send(message);
  }

  private onOrientation(event: OrientationLike): void {
    const now = this.deps.now();
    if (this.stopped || now - this.lastHeadingAt < HEADING_INTERVAL_MS) return;
    const message = toHeadingMessage(event, now);
    if (message === null) return;
    this.lastHeadingAt = now;
    this.update({ headingDeg: message.magnetic_deg });
    // Headings come ten times a second; under backpressure a newer one is coming anyway.
    this.send(message, true);
  }

  private onMotion(event: MotionLike): void {
    if (this.stopped) return;
    const now = this.deps.now();
    const result = takeBatch(addMotion(this.imu, event, now), now, IMU_FLUSH_MS, IMU_MAX_SAMPLES);
    this.imu = result.batch;
    if (result.message !== null) this.send(result.message);
  }
}
