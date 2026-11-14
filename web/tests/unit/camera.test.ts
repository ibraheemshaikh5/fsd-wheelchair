import { describe, expect, it } from 'vitest';
import {
  frameTimeMs,
  pickRearCamera,
  scaledSize,
  shouldGrabFallbackFrame,
  videoConstraints,
} from '../../capture/camera';

const device = (label: string, deviceId = label) => ({ kind: 'videoinput', deviceId, label });

describe('pickRearCamera', () => {
  it('prefers the rear ultra-wide camera for its wider view', () => {
    const devices = [
      device('Front Camera'),
      device('Back Camera'),
      device('Back Ultra Wide Camera'),
      device('Back Dual Wide Camera'),
    ];
    expect(pickRearCamera(devices)?.label).toBe('Back Ultra Wide Camera');
  });

  it('falls back to a plain rear camera, avoiding telephoto and multi-camera devices', () => {
    const devices = [device('Back Telephoto Camera'), device('Back Triple Camera'), device('Back Camera')];
    expect(pickRearCamera(devices)?.label).toBe('Back Camera');
  });

  it('ignores microphones and front cameras', () => {
    const devices = [{ kind: 'audioinput', deviceId: 'mic', label: 'Back Microphone' }, device('Front Camera')];
    expect(pickRearCamera(devices)).toBeNull();
  });

  it('returns null when labels are hidden before permission is granted', () => {
    expect(pickRearCamera([device('', 'abc')])).toBeNull();
  });
});

describe('videoConstraints', () => {
  it('asks for the environment camera when no device is chosen', () => {
    expect(videoConstraints(null, { width: 640, height: 480, fps: 10 })).toEqual({
      facingMode: { ideal: 'environment' },
      width: { ideal: 640 },
      height: { ideal: 480 },
      frameRate: { ideal: 10 },
    });
  });

  it('pins a specific device when one is chosen', () => {
    const constraints = videoConstraints('cam-1', { width: 640, height: 480, fps: 10 });
    expect(constraints.deviceId).toEqual({ exact: 'cam-1' });
    expect(constraints.facingMode).toBeUndefined();
  });
});

describe('scaledSize', () => {
  it('keeps size when already within the limit', () => {
    expect(scaledSize(640, 480, 640)).toEqual({ w: 640, h: 480 });
  });

  it('scales down preserving aspect ratio', () => {
    expect(scaledSize(1920, 1080, 640)).toEqual({ w: 640, h: 360 });
  });

  it('limits the long side for portrait video too', () => {
    expect(scaledSize(1080, 1920, 640)).toEqual({ w: 360, h: 640 });
  });

  it('never returns a zero dimension', () => {
    expect(scaledSize(0, 0, 640)).toEqual({ w: 1, h: 1 });
  });
});

describe('frameTimeMs', () => {
  // Wall clock (Date.now) and monotonic clock (performance.now) at the callback.
  const WALL_NOW = 2_000_000;
  const MONO_NOW = 1_000;

  it('subtracts the callback delay from the wall clock, using the capture time', () => {
    expect(frameTimeMs({ captureTime: 960, expectedDisplayTime: 990 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW - 40);
  });

  it('falls back to the display time', () => {
    expect(frameTimeMs({ expectedDisplayTime: 990 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW - 10);
  });

  it('falls back to now without metadata', () => {
    expect(frameTimeMs(undefined, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
  });

  it('never stamps a frame later than now (display time can be the next vsync)', () => {
    expect(frameTimeMs({ expectedDisplayTime: 1_016 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
  });

  it('stays on the wall clock when the monotonic clock has drifted (it stops while the phone sleeps)', () => {
    // performance.timeOrigin + performance.now() is 60 s behind Date.now() after a sleep.
    const timeOrigin = 1_939_000;
    expect(timeOrigin + MONO_NOW).toBe(WALL_NOW - 60_000);
    expect(frameTimeMs({ captureTime: 980 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW - 20);
  });
});

describe('shouldGrabFallbackFrame', () => {
  const playing = { paused: false, readyState: 4, muted: false, trackState: 'live' as const };

  it('grabs a frame only from a playing, unmuted, live camera', () => {
    expect(shouldGrabFallbackFrame(playing)).toBe(true);
    expect(shouldGrabFallbackFrame({ ...playing, readyState: 2 })).toBe(true);
  });

  it('never grabs a frozen picture', () => {
    expect(shouldGrabFallbackFrame({ ...playing, paused: true })).toBe(false);
    expect(shouldGrabFallbackFrame({ ...playing, readyState: 1 })).toBe(false);
    expect(shouldGrabFallbackFrame({ ...playing, muted: true })).toBe(false);
    expect(shouldGrabFallbackFrame({ ...playing, trackState: 'ended' })).toBe(false);
  });
});
