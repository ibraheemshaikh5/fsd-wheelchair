import { describe, expect, it } from 'vitest';
import { achievedFps, frameIntervalMs, isFrameDue, nextDueMs } from '../../capture/frame-rate';

describe('achievedFps', () => {
  it('reports the rate the camera track actually runs at', () => {
    expect(achievedFps(59.94, 60)).toBe(59.94);
    expect(achievedFps(30, 60)).toBe(30);
  });

  it('falls back to the usual 30 fps camera default, or a lower request, when the browser reports none or nonsense', () => {
    expect(achievedFps(undefined, 240)).toBe(30);
    expect(achievedFps(0, 240)).toBe(30);
    expect(achievedFps(Number.NaN, 60)).toBe(30);
    expect(achievedFps(-5, 60)).toBe(30);
    // Above anything a phone camera does (and what the service accepts).
    expect(achievedFps(1000, 60)).toBe(30);
    expect(achievedFps(undefined, 20)).toBe(20);
  });

  it('believes a camera running at up to 240 fps', () => {
    expect(achievedFps(240, 240)).toBe(240);
    expect(achievedFps(120, 240)).toBe(120);
  });
});

describe('frameIntervalMs', () => {
  it('takes every camera frame when the target is at or above the camera rate', () => {
    expect(frameIntervalMs(60, 60)).toBeNull();
    expect(frameIntervalMs(60, 30)).toBeNull();
    expect(frameIntervalMs(60, 59.94)).toBeNull();
  });

  it('thins frames when the target is below the camera rate', () => {
    expect(frameIntervalMs(20, 60)).toBe(50);
    expect(frameIntervalMs(10, 30)).toBe(100);
  });
});

describe('isFrameDue', () => {
  it('takes every frame without an interval, however close together', () => {
    expect(isFrameDue(1000, 1001, null)).toBe(true);
    expect(isFrameDue(null, 0, null)).toBe(true);
  });

  it('takes the first frame', () => {
    expect(isFrameDue(null, 0, 50)).toBe(true);
  });

  it('allows a frame arriving a little early, but not a whole camera frame early', () => {
    expect(isFrameDue(1050, 1049, 50)).toBe(true);
    expect(isFrameDue(1050, 1033.3, 50)).toBe(false);
  });
});

describe('nextDueMs', () => {
  it('schedules the frame after the first one a full interval later', () => {
    expect(nextDueMs(null, 1000, 50)).toBe(1050);
  });

  it('keeps to the schedule when a frame arrives late, instead of restarting from its arrival', () => {
    expect(nextDueMs(1050, 1066.7, 50)).toBe(1100);
  });

  it('catches up by at most one frame after a gap', () => {
    expect(nextDueMs(1050, 2000, 50)).toBe(2000);
  });
});

/** Frames taken in one second of a camera at cameraFps, thinned to targetFps. */
function framesTakenInOneSecond(cameraFps: number, targetFps: number, jitterMs = 0): number {
  const interval = frameIntervalMs(targetFps, cameraFps);
  const cameraFrames = Array.from({ length: Math.round(cameraFps) }, (_, k) => {
    const wobble = k % 2 === 0 ? jitterMs : -jitterMs;
    return (k * 1000) / cameraFps + wobble;
  });
  const { taken } = cameraFrames.reduce<{ due: number | null; taken: number }>(
    (state, now) =>
      isFrameDue(state.due, now, interval)
        ? { due: interval === null ? null : nextDueMs(state.due, now, interval), taken: state.taken + 1 }
        : state,
    { due: null, taken: 0 },
  );
  return taken;
}

describe('thinning a camera to a target rate', () => {
  it.each([
    [60, 20],
    [60, 50],
    [60, 40],
    [60, 25],
    [60, 30],
    [24, 20],
    [30, 10],
  ])('thins %d fps to an average of %d fps', (cameraFps, targetFps) => {
    expect(Math.abs(framesTakenInOneSecond(cameraFps, targetFps) - targetFps)).toBeLessThanOrEqual(1);
  });

  it('keeps the target rate when the camera callbacks jitter', () => {
    expect(Math.abs(framesTakenInOneSecond(60, 50, 1.5) - 50)).toBeLessThanOrEqual(1);
    expect(Math.abs(framesTakenInOneSecond(60, 20, 1.5) - 20)).toBeLessThanOrEqual(1);
  });

  it('takes every frame when the target is the camera rate', () => {
    expect(framesTakenInOneSecond(60, 60)).toBe(60);
  });
});
