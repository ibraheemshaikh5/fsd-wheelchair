// Fakes and a harness shared by the Session unit tests.
import { vi } from 'vitest';
import type { PhoneMessage } from '../../shared/protocol';
import { type CaptureSettings, DEFAULT_SETTINGS } from '../../capture/settings';
import {
  Session,
  type SessionDeps,
  type StreamLike,
  type TrackLike,
  type TransportHandlers,
  type TransportLike,
} from '../../capture/session';
import type { TransportState } from '../../capture/transport';

export interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

export function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => {};
  let reject: (error: unknown) => void = () => {};
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

type TrackEvent = 'mute' | 'unmute' | 'ended';

export class FakeTrack implements TrackLike {
  readonly stop = vi.fn();
  private readonly listeners = new Map<TrackEvent, Set<() => void>>();

  addEventListener(type: TrackEvent, listener: () => void): void {
    const set = this.listeners.get(type) ?? new Set();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: TrackEvent, listener: () => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  fire(type: TrackEvent): void {
    this.listeners.get(type)?.forEach((listener) => listener());
  }

  listenerCount(): number {
    return [...this.listeners.values()].reduce((total, set) => total + set.size, 0);
  }
}

export class FakeStream implements StreamLike {
  readonly track = new FakeTrack();
  getTracks(): readonly FakeTrack[] {
    return [this.track];
  }
  getVideoTracks(): readonly FakeTrack[] {
    return [this.track];
  }
}

export class FakeTransport implements TransportLike {
  readonly start = vi.fn();
  readonly stop = vi.fn();
  readonly sendJson = vi.fn((_message: unknown, _droppable?: boolean) => true);
  readonly sendFrame = vi.fn((_bytes: Uint8Array<ArrayBuffer>) => true);
  readonly checkLiveness = vi.fn();
  constructor(readonly handlers: TransportHandlers) {}

  state(state: TransportState): void {
    this.handlers.onState(state);
  }

  sentTypes(): string[] {
    return this.sendJson.mock.calls.map(([message]) => (message as PhoneMessage).type);
  }
}

export const OPEN: TransportState = { status: 'open', attempt: 0, lastClose: null };

export interface Harness {
  readonly session: Session<FakeStream>;
  readonly deps: SessionDeps<FakeStream>;
  readonly camera: Deferred<{ stream: FakeStream; label: string }>;
  readonly play: Deferred<{ width: number; height: number; frameRate?: number }>;
  readonly stream: FakeStream;
  readonly transport: () => FakeTransport;
  readonly callbacks: { onCamera: ReturnType<typeof vi.fn>; onTakenOver: ReturnType<typeof vi.fn>; onError: ReturnType<typeof vi.fn> };
  readonly frame: { onFrame: ((metadata: { captureTime?: number } | undefined) => void) | null };
  readonly sensors: {
    position: ((p: Parameters<Parameters<SessionDeps<FakeStream>['watchLocation']>[0]>[0]) => void) | null;
    locationError: ((message: string) => void) | null;
    orientation: ((e: Parameters<Parameters<SessionDeps<FakeStream>['listenOrientation']>[0]>[0]) => void) | null;
    motion: ((e: Parameters<Parameters<SessionDeps<FakeStream>['listenMotion']>[0]>[0]) => void) | null;
    wakeLock: ((held: boolean) => void) | null;
    tick: (() => void) | null;
  };
  setNow(ms: number): void;
  /** Set the monotonic clock (performance.now); 5_000 until set. */
  setMonotonic(ms: number): void;
}

export function harness(settings: CaptureSettings = DEFAULT_SETTINGS): Harness {
  const camera = deferred<{ stream: FakeStream; label: string }>();
  const play = deferred<{ width: number; height: number; frameRate?: number }>();
  const stream = new FakeStream();
  let transport: FakeTransport | null = null;
  let now = 1_000_000;
  let monotonic = 5_000;
  const frame: Harness['frame'] = { onFrame: null };
  const sensors: Harness['sensors'] = {
    position: null,
    locationError: null,
    orientation: null,
    motion: null,
    wakeLock: null,
    tick: null,
  };
  const deps: SessionDeps<FakeStream> = {
    openCamera: vi.fn(() => camera.promise),
    playPreview: vi.fn(() => play.promise),
    clearPreview: vi.fn(),
    resumePreview: vi.fn(() => Promise.resolve()),
    createTransport: (handlers) => {
      transport = new FakeTransport(handlers);
      return transport;
    },
    startFrameLoop: vi.fn((_intervalMs: number | null, onFrame) => {
      frame.onFrame = onFrame;
      return vi.fn();
    }),
    encodeFrame: vi.fn(() => Promise.resolve({ bytes: new Uint8Array([1, 2]), w: 2, h: 1, rot: 0 as const })),
    watchLocation: vi.fn((onPosition, onError) => {
      sensors.position = onPosition;
      sensors.locationError = onError;
      return vi.fn();
    }),
    keepScreenOn: vi.fn((onChange) => {
      sensors.wakeLock = onChange;
      return vi.fn();
    }),
    listenOrientation: vi.fn((onReading) => {
      sensors.orientation = onReading;
      return vi.fn();
    }),
    listenMotion: vi.fn((onMotion) => {
      sensors.motion = onMotion;
      return vi.fn();
    }),
    setInterval: vi.fn((fn: () => void) => {
      sensors.tick = fn;
      return 7;
    }),
    clearInterval: vi.fn(),
    now: () => now,
    monotonicNow: () => monotonic,
    userAgent: 'test-agent',
  };
  const callbacks = { onCamera: vi.fn(), onTakenOver: vi.fn(), onError: vi.fn() };
  const session = new Session(settings, deps, callbacks);
  return {
    session,
    deps,
    camera,
    play,
    stream,
    transport: () => {
      if (transport === null) throw new Error('no transport created');
      return transport;
    },
    callbacks,
    frame,
    sensors,
    setNow: (ms) => {
      now = ms;
    },
    setMonotonic: (ms) => {
      monotonic = ms;
    },
  };
}

export async function startFully(h: Harness, motion: 'granted' | 'denied' | 'not-needed' = 'granted'): Promise<void> {
  const started = h.session.start(Promise.resolve(motion));
  h.camera.resolve({ stream: h.stream, label: 'Back Ultra Wide Camera' });
  h.play.resolve({ width: 1280, height: 720 });
  await started;
}
