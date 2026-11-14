import { describe, expect, it } from 'vitest';
import { monotonicToWallMs, sampleFromPong } from '../../capture/clock';
import { toHeadingMessage } from '../../capture/heading';
import { recentFix, toGpsMessage } from '../../capture/location';
import { addMotion, drainBatch, EMPTY_BATCH, IMU_AXES, motionTimeMs, takeBatch } from '../../capture/motion';

const NOW = 1_700_000_000_000;

describe('sampleFromPong', () => {
  it('computes round trip and offset assuming a symmetric network', () => {
    // Sent at 1000 (phone), server stamped 5050, received at 1100 (phone).
    const sample = sampleFromPong({ v: 1, type: 'pong', t: 1000, server_t: 5050 }, 1100);
    expect(sample).toEqual({ rtt_ms: 100, offset_ms: 4000 });
  });

  it('ignores a pong that arrives before it was sent', () => {
    expect(sampleFromPong({ v: 1, type: 'pong', t: 1000, server_t: 1 }, 999)).toBeNull();
  });
});

describe('monotonicToWallMs', () => {
  const WALL_NOW = 2_000_000;
  const MONO_NOW = 1_000;

  it('subtracts the delay measured on the monotonic clock from the wall clock', () => {
    expect(monotonicToWallMs(960, WALL_NOW, MONO_NOW)).toBe(WALL_NOW - 40);
  });

  it('never stamps a time later than now', () => {
    expect(monotonicToWallMs(1_016, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
  });

  it('falls back to now with no time, or one older than the allowed delay', () => {
    expect(monotonicToWallMs(undefined, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
    expect(monotonicToWallMs(Number.NaN, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
    expect(monotonicToWallMs(0, WALL_NOW, MONO_NOW, 500)).toBe(WALL_NOW);
    expect(monotonicToWallMs(600, WALL_NOW, MONO_NOW, 500)).toBe(WALL_NOW - 400);
  });
});

describe('toGpsMessage', () => {
  const position = (coords: Partial<GeolocationCoordinates>, timestamp = NOW - 200) => ({
    coords: {
      latitude: 38.98,
      longitude: -76.94,
      accuracy: 5,
      altitude: 40,
      speed: 1.3,
      heading: 92,
      ...coords,
    },
    timestamp,
  });

  it('maps a full fix', () => {
    expect(toGpsMessage(position({}), NOW)).toEqual({
      v: 1,
      type: 'gps',
      t: NOW,
      lat: 38.98,
      lon: -76.94,
      accuracy_m: 5,
      altitude_m: 40,
      speed_mps: 1.3,
      course_deg: 92,
      fix_t: NOW - 200,
    });
  });

  it.each([
    ['null', null],
    ['NaN', Number.NaN],
    ['negative', -1],
  ])('turns a %s speed and course into null', (_label, value) => {
    const message = toGpsMessage(position({ speed: value, heading: value }), NOW);
    expect(message.speed_mps).toBeNull();
    expect(message.course_deg).toBeNull();
  });

  it('wraps a course of 360 to 0', () => {
    expect(toGpsMessage(position({ heading: 360 }), NOW).course_deg).toBe(0);
  });

  it('keeps a missing altitude as null', () => {
    expect(toGpsMessage(position({ altitude: null }), NOW).altitude_m).toBeNull();
  });

  it('falls back to now when the fix has no usable timestamp', () => {
    expect(toGpsMessage(position({}, 0), NOW).fix_t).toBe(NOW);
  });
});

describe('recentFix', () => {
  const fix = toGpsMessage(
    {
      coords: { latitude: 1, longitude: 2, accuracy: 3, altitude: null, speed: null, heading: null },
      timestamp: NOW,
    },
    NOW,
  );

  it('re-stamps a fix young enough to resend after reconnecting, keeping its fix time', () => {
    const resent = recentFix(fix, NOW + 1500, 2000);
    expect(resent).toEqual({ ...fix, t: NOW + 1500 });
    expect(resent?.fix_t).toBe(NOW);
  });

  it('drops a fix that is too old to pass off as current', () => {
    expect(recentFix(fix, NOW + 2001, 2000)).toBeNull();
  });

  it('handles having no fix yet', () => {
    expect(recentFix(null, NOW, 2000)).toBeNull();
  });
});

describe('toHeadingMessage', () => {
  it('prefers the iOS compass heading', () => {
    const event = { webkitCompassHeading: 123.4, webkitCompassAccuracy: 12, alpha: 5, absolute: false };
    expect(toHeadingMessage(event, NOW)).toEqual({
      v: 1,
      type: 'heading',
      t: NOW,
      magnetic_deg: 123.4,
      accuracy_deg: 12,
    });
  });

  it('reports unknown accuracy when iOS says the compass is uncalibrated', () => {
    const event = { webkitCompassHeading: 10, webkitCompassAccuracy: -1, alpha: null, absolute: false };
    expect(toHeadingMessage(event, NOW)?.accuracy_deg).toBeNull();
  });

  it('converts an absolute alpha into a clockwise heading', () => {
    expect(toHeadingMessage({ alpha: 90, absolute: true }, NOW)?.magnetic_deg).toBe(270);
    expect(toHeadingMessage({ alpha: 0, absolute: true }, NOW)?.magnetic_deg).toBe(0);
  });

  it('returns nothing for a relative alpha with no compass', () => {
    expect(toHeadingMessage({ alpha: 90, absolute: false }, NOW)).toBeNull();
  });

  it('returns nothing when there is no reading at all', () => {
    expect(toHeadingMessage({ alpha: null, absolute: true }, NOW)).toBeNull();
    expect(toHeadingMessage({ webkitCompassHeading: Number.NaN, alpha: null, absolute: false }, NOW)).toBeNull();
  });
});

describe('IMU batching', () => {
  const motion = {
    accelerationIncludingGravity: { x: 0.1, y: 9.7, z: 0.3 },
    rotationRate: { alpha: 3, beta: 1, gamma: 2 },
  };

  it('labels the rotation columns as rotation about device x, y, z', () => {
    expect(IMU_AXES).toBe('device_xyz');
  });

  it('stamps samples relative to the first one and maps iOS rotation axes to x, y, z', () => {
    // On iOS rotationRate.alpha is about device x, beta about y, gamma about z.
    const batch = addMotion(addMotion(EMPTY_BATCH, motion, 1000), motion, 1016);
    expect(batch.startMs).toBe(1000);
    expect(batch.samples).toEqual([
      [0, 0.1, 9.7, 0.3, 3, 1, 2],
      [16, 0.1, 9.7, 0.3, 3, 1, 2],
    ]);
  });

  it('keeps each rotation axis in its own column', () => {
    const aboutX = addMotion(EMPTY_BATCH, { ...motion, rotationRate: { alpha: 5, beta: 0, gamma: 0 } }, 1);
    const aboutY = addMotion(EMPTY_BATCH, { ...motion, rotationRate: { alpha: 0, beta: 5, gamma: 0 } }, 1);
    const aboutZ = addMotion(EMPTY_BATCH, { ...motion, rotationRate: { alpha: 0, beta: 0, gamma: 5 } }, 1);
    expect(aboutX.samples[0]?.slice(4)).toEqual([5, 0, 0]);
    expect(aboutY.samples[0]?.slice(4)).toEqual([0, 5, 0]);
    expect(aboutZ.samples[0]?.slice(4)).toEqual([0, 0, 5]);
  });

  it('always sends seven numbers per sample', () => {
    const batch = addMotion(EMPTY_BATCH, motion, 1000);
    expect(batch.samples[0]).toHaveLength(7);
  });

  it('never lets sample times run backwards (the wall clock can be stepped)', () => {
    const batch = addMotion(addMotion(addMotion(EMPTY_BATCH, motion, 1000), motion, 1020), motion, 1010);
    expect(batch.samples.map((sample) => sample[0])).toEqual([0, 20, 20]);
  });

  it('does not change the batch it was given', () => {
    addMotion(EMPTY_BATCH, motion, 1000);
    expect(EMPTY_BATCH.samples).toEqual([]);
    expect(EMPTY_BATCH.startMs).toBeNull();
  });

  it('skips events without acceleration and fills missing rotation with zeros', () => {
    const noAccel = addMotion(EMPTY_BATCH, { accelerationIncludingGravity: null, rotationRate: null }, 1);
    expect(noAccel).toBe(EMPTY_BATCH);
    const noRotation = addMotion(EMPTY_BATCH, { ...motion, rotationRate: null }, 1);
    expect(noRotation.samples[0]).toEqual([0, 0.1, 9.7, 0.3, 0, 0, 0]);
  });

  it('holds samples until the flush interval passes, then emits one message', () => {
    const batch = addMotion(EMPTY_BATCH, motion, 1000);
    const early = takeBatch(batch, 1040, 50, 200);
    expect(early.message).toBeNull();
    expect(early.batch).toBe(batch);

    const due = takeBatch(batch, 1050, 50, 200);
    expect(due.message).toEqual({ v: 1, type: 'imu', t: 1000, samples: [[0, 0.1, 9.7, 0.3, 3, 1, 2]] });
    expect(due.batch).toBe(EMPTY_BATCH);
  });

  it('flushes early when the batch is full', () => {
    let batch = EMPTY_BATCH;
    for (let i = 0; i < 3; i += 1) batch = addMotion(batch, motion, 1000 + i);
    expect(takeBatch(batch, 1002, 100, 3).message?.samples).toHaveLength(3);
  });

  it('emits nothing for an empty batch', () => {
    expect(takeBatch(EMPTY_BATCH, 99_999, 100, 200).message).toBeNull();
    expect(drainBatch(EMPTY_BATCH).message).toBeNull();
  });

  it('drains a pending batch at once, however young', () => {
    const batch = addMotion(EMPTY_BATCH, motion, 1000);
    const drained = drainBatch(batch);
    expect(drained.message?.samples).toHaveLength(1);
    expect(drained.batch).toBe(EMPTY_BATCH);
  });
});

describe('motionTimeMs', () => {
  const WALL_NOW = 2_000_000;
  const MONO_NOW = 1_000;

  it('stamps a sample when the sensor measured it, not when the handler ran', () => {
    expect(motionTimeMs({ timeStamp: 990 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW - 10);
  });

  it('falls back to now without a usable event time', () => {
    expect(motionTimeMs({}, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
    // An epoch-style timeStamp from an old engine is far in the "future" of the monotonic clock.
    expect(motionTimeMs({ timeStamp: 1_700_000_000_000 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
    // A timeStamp from a different origin looks seconds old.
    expect(motionTimeMs({ timeStamp: -60_000 }, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
  });
});
