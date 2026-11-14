import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PhoneMessage } from '../../shared/protocol';
import { DEFAULT_SETTINGS } from '../../capture/settings';
import {
  type CameraChange,
  LIVENESS_TIMEOUT_MS,
  Session,
  SessionCancelled,
  type SessionDeps,
  type StreamLike,
  type TrackLike,
  type TransportHandlers,
  type TransportLike,
} from '../../capture/session';
import { CLOSE_REPLACED, type TransportState } from '../../capture/transport';

interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => {};
  let reject: (error: unknown) => void = () => {};
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

type TrackEvent = 'mute' | 'unmute' | 'ended';

class FakeTrack implements TrackLike {
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

class FakeStream implements StreamLike {
  readonly track = new FakeTrack();
  getTracks(): readonly FakeTrack[] {
    return [this.track];
  }
  getVideoTracks(): readonly FakeTrack[] {
    return [this.track];
  }
}

class FakeTransport implements TransportLike {
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

const OPEN: TransportState = { status: 'open', attempt: 0, lastClose: null };

interface Harness {
  readonly session: Session<FakeStream>;
  readonly deps: SessionDeps<FakeStream>;
  readonly camera: Deferred<{ stream: FakeStream; label: string }>;
  readonly play: Deferred<{ width: number; height: number }>;
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
}

function harness(): Harness {
  const camera = deferred<{ stream: FakeStream; label: string }>();
  const play = deferred<{ width: number; height: number }>();
  const stream = new FakeStream();
  let transport: FakeTransport | null = null;
  let now = 1_000_000;
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
    startFrameLoop: vi.fn((_fps: number, onFrame) => {
      frame.onFrame = onFrame;
      return vi.fn();
    }),
    encodeFrame: vi.fn(() => Promise.resolve({ bytes: new Uint8Array([1, 2]), w: 2, h: 1 })),
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
    monotonicNow: () => 5_000,
    userAgent: 'test-agent',
  };
  const callbacks = { onCamera: vi.fn(), onTakenOver: vi.fn(), onError: vi.fn() };
  const session = new Session(DEFAULT_SETTINGS, deps, callbacks);
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
  };
}

async function startFully(h: Harness, motion: 'granted' | 'denied' | 'not-needed' = 'granted'): Promise<void> {
  const started = h.session.start(Promise.resolve(motion));
  h.camera.resolve({ stream: h.stream, label: 'Back Ultra Wide Camera' });
  h.play.resolve({ width: 1280, height: 720 });
  await started;
}

describe('Session start cancellation', () => {
  let h: Harness;
  beforeEach(() => {
    h = harness();
  });

  it('stops a camera stream that arrives after Stop and starts nothing else', async () => {
    const started = h.session.start(Promise.resolve('granted'));
    h.session.stop();
    h.camera.resolve({ stream: h.stream, label: 'cam' });
    await expect(started).rejects.toBeInstanceOf(SessionCancelled);
    expect(h.stream.track.stop).toHaveBeenCalledTimes(1);
    expect(h.transport().start).not.toHaveBeenCalled();
    expect(h.deps.playPreview).not.toHaveBeenCalled();
    expect(h.deps.startFrameLoop).not.toHaveBeenCalled();
    expect(h.deps.watchLocation).not.toHaveBeenCalled();
    expect(h.deps.keepScreenOn).not.toHaveBeenCalled();
    expect(h.deps.setInterval).not.toHaveBeenCalled();
  });

  it('aborts the camera request so a lens switch cannot leak a stream', async () => {
    const started = h.session.start(Promise.resolve('granted'));
    const signal = vi.mocked(h.deps.openCamera).mock.calls[0]?.[1];
    expect(signal?.aborted).toBe(false);
    h.session.stop();
    expect(signal?.aborted).toBe(true);
    h.camera.reject(new DOMException('aborted', 'AbortError'));
    await expect(started).rejects.toBeInstanceOf(SessionCancelled);
  });

  it('clears the preview and stops the stream when Stop comes during play()', async () => {
    const started = h.session.start(Promise.resolve('granted'));
    h.camera.resolve({ stream: h.stream, label: 'cam' });
    await flush();
    expect(h.deps.playPreview).toHaveBeenCalledWith(h.stream);
    h.session.stop();
    expect(h.deps.clearPreview).toHaveBeenCalledWith(h.stream);
    h.play.reject(new DOMException('interrupted', 'AbortError'));
    await expect(started).rejects.toBeInstanceOf(SessionCancelled);
    expect(h.stream.track.stop).toHaveBeenCalledTimes(1);
    expect(h.transport().start).not.toHaveBeenCalled();
    expect(h.stream.track.listenerCount()).toBe(0);
  });

  it('adds no motion listeners when Stop comes while motion access is pending', async () => {
    const motion = deferred<'granted' | 'denied' | 'not-needed'>();
    const started = h.session.start(motion.promise);
    h.camera.resolve({ stream: h.stream, label: 'cam' });
    h.play.resolve({ width: 1280, height: 720 });
    await flush();
    expect(h.transport().start).toHaveBeenCalledTimes(1);
    h.session.stop();
    motion.resolve('granted');
    await expect(started).rejects.toBeInstanceOf(SessionCancelled);
    expect(h.deps.listenOrientation).not.toHaveBeenCalled();
    expect(h.deps.listenMotion).not.toHaveBeenCalled();
  });

  it('passes a real camera failure through unchanged', async () => {
    const started = h.session.start(Promise.resolve('granted'));
    const failure = new DOMException('no', 'NotAllowedError');
    h.camera.reject(failure);
    await expect(started).rejects.toBe(failure);
  });

  it('is idempotent and undoes everything it started', async () => {
    await startFully(h);
    h.session.stop();
    h.session.stop();
    expect(h.transport().stop).toHaveBeenCalledTimes(1);
    expect(h.stream.track.stop).toHaveBeenCalledTimes(1);
    expect(h.deps.clearInterval).toHaveBeenCalledWith(7);
    expect(h.stream.track.listenerCount()).toBe(0);
  });
});

describe('Session streaming', () => {
  let h: Harness;
  beforeEach(async () => {
    h = harness();
    await startFully(h);
  });

  it('records the camera and starts every sensor once started', () => {
    expect(h.session.live.cameraLabel).toBe('Back Ultra Wide Camera');
    expect(h.session.live.motion).toBe('granted');
    expect(h.transport().start).toHaveBeenCalledTimes(1);
    expect(h.deps.listenOrientation).toHaveBeenCalledTimes(1);
    expect(h.deps.listenMotion).toHaveBeenCalledTimes(1);
  });

  it('sends hello and then a ping as soon as the socket opens', () => {
    h.transport().state(OPEN);
    expect(h.transport().sentTypes()).toEqual(['hello', 'ping']);
    const hello = h.transport().sendJson.mock.calls[0]?.[0] as PhoneMessage;
    expect(hello).toMatchObject({ type: 'hello', user_agent: 'test-agent', camera: { width: 1280, height: 720 } });
  });

  it('pings again on every reconnect, with the last clock sample', () => {
    h.transport().state(OPEN);
    h.transport().handlers.onMessage({ v: 1, type: 'pong', t: 1_000_000 - 40, server_t: 1_000_000 });
    h.transport().state({ status: 'waiting', attempt: 1, lastClose: { code: 1006, reason: '' } });
    h.transport().sendJson.mockClear();
    h.transport().state(OPEN);
    expect(h.transport().sentTypes()).toEqual(['hello', 'ping']);
    expect(h.transport().sendJson.mock.calls[1]?.[0]).toMatchObject({ type: 'ping', rtt_ms: 40 });
  });

  it('resends a recent fix after hello and ping, never as droppable', () => {
    h.sensors.position?.({
      coords: { latitude: 1, longitude: 2, accuracy: 3, altitude: null, speed: null, heading: null },
      timestamp: 1_000_000,
    });
    expect(h.session.live.gpsAccuracyM).toBe(3);
    h.transport().sendJson.mockClear();
    h.transport().state(OPEN);
    expect(h.transport().sentTypes()).toEqual(['hello', 'ping', 'gps']);
    for (const call of h.transport().sendJson.mock.calls) expect(call[1] ?? false).toBe(false);
  });

  it('checks the link is alive on every ping tick', () => {
    h.setNow(2_000_000);
    h.sensors.tick?.();
    expect(h.transport().checkLiveness).toHaveBeenCalledWith(2_000_000, LIVENESS_TIMEOUT_MS);
  });

  it('marks only heading messages as droppable under backpressure', () => {
    h.sensors.orientation?.({ webkitCompassHeading: 90, alpha: null, absolute: false });
    expect(h.session.live.headingDeg).toBe(90);
    expect(h.transport().sendJson).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'heading' }), true);
    // Throttled: a second reading within the interval is not sent.
    h.sensors.orientation?.({ webkitCompassHeading: 91, alpha: null, absolute: false });
    expect(h.transport().sendJson).toHaveBeenCalledTimes(1);
    // Not a compass reading at all.
    h.setNow(1_001_000);
    h.sensors.orientation?.({ alpha: null, absolute: false });
    expect(h.transport().sendJson).toHaveBeenCalledTimes(1);
  });

  it('batches motion into imu messages that are never dropped', () => {
    const event = { accelerationIncludingGravity: { x: 0, y: 0, z: 9.8 }, rotationRate: null };
    h.sensors.motion?.(event);
    h.setNow(1_000_200);
    h.sensors.motion?.(event);
    expect(h.transport().sendJson).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'imu' }), false);
  });

  it('reports the wake lock and location errors', () => {
    h.sensors.wakeLock?.(true);
    expect(h.session.live.wakeLock).toBe(true);
    h.sensors.locationError?.('No location fix available yet.');
    expect(h.callbacks.onError).toHaveBeenCalledWith('No location fix available yet.');
  });

  it('keeps the clock from pongs and reports service errors', () => {
    h.transport().handlers.onMessage({ v: 1, type: 'pong', t: 1_000_000 - 30, server_t: 1_000_000 });
    expect(h.session.live.clock?.rtt_ms).toBe(30);
    h.transport().handlers.onMessage({ v: 1, type: 'pong', t: 1_000_000 + 30, server_t: 1_000_000 });
    expect(h.session.live.clock?.rtt_ms).toBe(30);
    h.transport().handlers.onMessage({ v: 1, type: 'error', message: 'bad gps' });
    expect(h.callbacks.onError).toHaveBeenCalledWith('Service rejected a message: bad gps');
  });

  it('skips frames while the link is down and sends them once open', async () => {
    h.frame.onFrame?.(undefined);
    expect(h.session.live.counters).toEqual({ captured: 1, sent: 0, skipped: 1 });
    h.transport().state(OPEN);
    h.frame.onFrame?.({ captureTime: 100 });
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.session.live.counters).toEqual({ captured: 3, sent: 1, skipped: 2 });
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(1);
  });

  it('stamps frames on the wall clock even when the monotonic clock drifted 60 s', async () => {
    h.transport().state(OPEN);
    // now() is 1_000_000; monotonicNow() is 5_000. A timeOrigin-based stamp would
    // need timeOrigin = 995_000, which is wrong once the monotonic clock paused.
    h.frame.onFrame?.({ captureTime: 4_970 });
    await flush();
    const bytes = h.transport().sendFrame.mock.calls[0]?.[0];
    if (bytes === undefined) throw new Error('no frame sent');
    const length = new DataView(bytes.buffer).getUint32(0, true);
    const header = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length))) as { t: number };
    expect(header.t).toBe(1_000_000 - 30);
  });

  it('counts a frame the encoder or socket refused as skipped', async () => {
    h.transport().state(OPEN);
    vi.mocked(h.deps.encodeFrame).mockResolvedValueOnce(null);
    h.frame.onFrame?.(undefined);
    await flush();
    h.transport().sendFrame.mockReturnValueOnce(false);
    h.frame.onFrame?.(undefined);
    await flush();
    vi.mocked(h.deps.encodeFrame).mockRejectedValueOnce(new Error('canvas lost'));
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.session.live.counters).toEqual({ captured: 3, sent: 0, skipped: 3 });
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.session.live.counters.sent).toBe(1);
  });

  it('reports camera interruptions and stops listening once stopped', () => {
    const changes: CameraChange[] = ['muted', 'unmuted', 'ended'];
    h.stream.track.fire('mute');
    h.stream.track.fire('unmute');
    h.stream.track.fire('ended');
    expect(h.callbacks.onCamera.mock.calls.map(([change]) => change)).toEqual(changes);
    h.session.stop();
    h.stream.track.fire('mute');
    expect(h.callbacks.onCamera).toHaveBeenCalledTimes(3);
  });

  it('tells the page when another phone took over the stream', () => {
    h.transport().state({ status: 'stopped', attempt: 0, lastClose: { code: CLOSE_REPLACED, reason: 'replaced' } });
    expect(h.callbacks.onTakenOver).toHaveBeenCalledTimes(1);
    expect(h.session.live.connection.status).toBe('stopped');
  });

  it('ignores the transport once stopped so a zombie cannot write live state', () => {
    h.session.stop();
    h.transport().state(OPEN);
    h.transport().handlers.onMessage({ v: 1, type: 'error', message: 'late' });
    expect(h.session.live.connection.status).not.toBe('open');
    expect(h.transport().sendJson).not.toHaveBeenCalled();
    expect(h.callbacks.onError).not.toHaveBeenCalled();
  });

  it('replays the preview on request', async () => {
    await h.session.replayPreview();
    expect(h.deps.resumePreview).toHaveBeenCalledTimes(1);
  });
});

describe('Session without motion access', () => {
  it('does not listen for compass or IMU when motion access is denied', async () => {
    const h = harness();
    await startFully(h, 'denied');
    expect(h.session.live.motion).toBe('denied');
    expect(h.deps.listenOrientation).not.toHaveBeenCalled();
    expect(h.deps.listenMotion).not.toHaveBeenCalled();
  });

  it('falls back to the camera request size when the preview reports none', async () => {
    const h = harness();
    const started = h.session.start(Promise.resolve('not-needed'));
    h.camera.resolve({ stream: h.stream, label: 'cam' });
    h.play.resolve({ width: 0, height: 0 });
    await started;
    h.transport().state(OPEN);
    const hello = h.transport().sendJson.mock.calls[0]?.[0] as PhoneMessage;
    expect(hello).toMatchObject({ type: 'hello', camera: { width: 1280, height: 720 } });
  });
});
