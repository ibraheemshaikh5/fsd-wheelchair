import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PhoneMessage } from '../../shared/protocol';
import { DEFAULT_SETTINGS } from '../../capture/settings';
import { SessionCancelled } from '../../capture/session';
import { deferred, flush, harness, startFully, OPEN, type Harness } from './session-harness';

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

describe('Session without motion access', () => {
  it('does not listen for compass or IMU when motion access is denied', async () => {
    const h = harness();
    await startFully(h, 'denied');
    expect(h.session.live.motion).toBe('denied');
    expect(h.deps.listenOrientation).not.toHaveBeenCalled();
    expect(h.deps.listenMotion).not.toHaveBeenCalled();
  });

  it('reports the frame rate the camera achieved in hello, and thins frames only above the target', async () => {
    const h = harness({ ...DEFAULT_SETTINGS, fps: 60 });
    const started = h.session.start(Promise.resolve('not-needed'));
    h.camera.resolve({ stream: h.stream, label: 'cam' });
    h.play.resolve({ width: 1280, height: 720, frameRate: 30 });
    await started;
    expect(h.deps.startFrameLoop).toHaveBeenCalledWith(null, expect.any(Function));
    h.transport().state(OPEN);
    expect(h.transport().sendJson.mock.calls[0]?.[0]).toMatchObject({ type: 'hello', camera: { fps: 30 } });
  });

  it('thins a faster camera down to the configured rate', async () => {
    const h = harness({ ...DEFAULT_SETTINGS, fps: 20 });
    const started = h.session.start(Promise.resolve('not-needed'));
    h.camera.resolve({ stream: h.stream, label: 'cam' });
    h.play.resolve({ width: 1280, height: 720, frameRate: 60 });
    await started;
    expect(h.deps.startFrameLoop).toHaveBeenCalledWith(50, expect.any(Function));
    h.transport().state(OPEN);
    expect(h.transport().sendJson.mock.calls[0]?.[0]).toMatchObject({ type: 'hello', camera: { fps: 20 } });
  });

  it('reports the usual camera default when the camera does not say what it achieved', async () => {
    const h = harness({ ...DEFAULT_SETTINGS, fps: 60 });
    await startFully(h, 'not-needed');
    h.transport().state(OPEN);
    expect(h.transport().sendJson.mock.calls[0]?.[0]).toMatchObject({ type: 'hello', camera: { fps: 30 } });
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
