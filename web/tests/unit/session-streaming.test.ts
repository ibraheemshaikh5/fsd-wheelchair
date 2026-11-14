import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PhoneMessage } from '../../shared/protocol';
import { type CameraChange, LIVENESS_TIMEOUT_MS } from '../../capture/session';
import { DEFAULT_SETTINGS } from '../../capture/settings';
import { BUSY_TEXT, TAKEOVER_TEXT } from '../../capture/status';
import { CLOSE_BUSY, CLOSE_REPLACED } from '../../capture/transport';
import { deferred, flush, harness, startFully, OPEN, type Harness } from './session-harness';

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

  it('says in hello that the IMU rotation columns are about device x, y, z', () => {
    h.transport().state(OPEN);
    expect(h.transport().sendJson.mock.calls[0]?.[0]).toMatchObject({ type: 'hello', imu_axes: 'device_xyz' });
  });

  it('asks the camera for the configured rate and takes every frame it delivers', () => {
    expect(h.deps.openCamera).toHaveBeenCalledWith(
      expect.objectContaining({ fps: DEFAULT_SETTINGS.fps }),
      expect.anything(),
    );
    expect(h.deps.startFrameLoop).toHaveBeenCalledWith(null, expect.any(Function));
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

  it('sends compass headings at up to 30 a second', () => {
    const reading = { webkitCompassHeading: 90, alpha: null, absolute: false };
    h.sensors.orientation?.(reading);
    h.setNow(1_000_016);
    h.sensors.orientation?.(reading);
    h.setNow(1_000_033);
    h.sensors.orientation?.(reading);
    h.setNow(1_000_066);
    h.sensors.orientation?.(reading);
    expect(h.transport().sentTypes()).toEqual(['heading', 'heading', 'heading']);
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

  it('batches motion into imu messages every 50 ms that are never dropped', () => {
    const event = { accelerationIncludingGravity: { x: 0, y: 0, z: 9.8 }, rotationRate: null };
    h.sensors.motion?.(event);
    h.setNow(1_000_040);
    h.sensors.motion?.(event);
    expect(h.transport().sentTypes()).toEqual([]);
    h.setNow(1_000_050);
    h.sensors.motion?.(event);
    expect(h.transport().sendJson).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'imu' }), false);
  });

  it('stamps IMU samples with the event time on the wall clock', () => {
    // now() is 1_000_000 and monotonicNow() is 5_000, so timeStamp 4_990 was 10 ms ago.
    const event = { accelerationIncludingGravity: { x: 0, y: 0, z: 9.8 }, rotationRate: null, timeStamp: 4_990 };
    h.sensors.motion?.(event);
    h.session.stop();
    expect(h.transport().sendJson).toHaveBeenLastCalledWith(
      expect.objectContaining({ type: 'imu', t: 1_000_000 - 10 }),
      false,
    );
  });

  it('sends the pending IMU batch on stop, before closing the link', () => {
    const event = { accelerationIncludingGravity: { x: 0, y: 0, z: 9.8 }, rotationRate: null };
    h.sensors.motion?.(event);
    expect(h.transport().sentTypes()).toEqual([]);
    h.session.stop();
    expect(h.transport().sentTypes()).toEqual(['imu']);
    const sentAt = h.transport().sendJson.mock.invocationCallOrder[0] ?? 0;
    const stoppedAt = h.transport().stop.mock.invocationCallOrder[0] ?? 0;
    expect(sentAt).toBeLessThan(stoppedAt);
  });

  it('sends nothing extra on stop when no IMU samples are pending', () => {
    h.session.stop();
    expect(h.transport().sendJson).not.toHaveBeenCalled();
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
    expect(h.session.live.counters).toEqual({ captured: 3, sent: 2, skipped: 1 });
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(2);
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

  it('keeps a second frame encoding while the first finishes, so a slow encode does not halve the rate', async () => {
    h.transport().state(OPEN);
    const first = deferred<{ bytes: Uint8Array<ArrayBuffer>; w: number; h: number; rot: 0 } | null>();
    const second = deferred<{ bytes: Uint8Array<ArrayBuffer>; w: number; h: number; rot: 0 } | null>();
    vi.mocked(h.deps.encodeFrame).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    h.frame.onFrame?.(undefined);
    h.frame.onFrame?.(undefined);
    // A third while two are in flight is dropped: latest-wins, no queue.
    h.frame.onFrame?.(undefined);
    expect(h.deps.encodeFrame).toHaveBeenCalledTimes(2);
    first.resolve({ bytes: new Uint8Array([1]), w: 1, h: 1, rot: 0 });
    second.resolve({ bytes: new Uint8Array([2]), w: 1, h: 1, rot: 0 });
    await flush();
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(2);
    expect(h.session.live.counters).toEqual({ captured: 3, sent: 2, skipped: 1 });
  });

  it('drops an older frame that finishes encoding after a newer one was sent', async () => {
    h.transport().state(OPEN);
    const first = deferred<{ bytes: Uint8Array<ArrayBuffer>; w: number; h: number; rot: 0 } | null>();
    const second = deferred<{ bytes: Uint8Array<ArrayBuffer>; w: number; h: number; rot: 0 } | null>();
    vi.mocked(h.deps.encodeFrame).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    h.frame.onFrame?.(undefined);
    h.frame.onFrame?.(undefined);
    second.resolve({ bytes: new Uint8Array([2]), w: 1, h: 1, rot: 0 });
    await flush();
    first.resolve({ bytes: new Uint8Array([1]), w: 1, h: 1, rot: 0 });
    await flush();
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(1);
    expect(h.session.live.counters).toEqual({ captured: 2, sent: 1, skipped: 1 });
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
    expect(h.callbacks.onTakenOver).toHaveBeenCalledWith(TAKEOVER_TEXT);
    expect(h.session.live.connection.status).toBe('stopped');
  });

  it('tells the page when a priority phone turned this one away', () => {
    h.transport().state({ status: 'stopped', attempt: 0, lastClose: { code: CLOSE_BUSY, reason: 'busy' } });
    expect(h.callbacks.onTakenOver).toHaveBeenCalledWith(BUSY_TEXT);
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
