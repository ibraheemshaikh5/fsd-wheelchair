import { describe, expect, it } from 'vitest';
import { parseStatus, type MotionStatus, type ServiceStatus } from '../../shared/status';
import { statusRows } from '../../viewer/format';
import { motionRows } from '../../viewer/motion';

const MOTION: MotionStatus = {
  speed_mps: 1.4,
  speed_source: 'imu',
  yaw_rate_dps: 12.5,
  accel_fwd_mps2: 0.3,
  accel_lat_mps2: -0.1,
  age_ms: 40,
  fresh: true,
};

const UNKNOWN: MotionStatus = {
  speed_mps: null,
  speed_source: null,
  yaw_rate_dps: null,
  accel_fwd_mps2: null,
  accel_lat_mps2: null,
  age_ms: null,
  fresh: false,
};

const status = (motion: unknown): Record<string, unknown> => ({
  type: 'status',
  phone_connected: true,
  stats: { fps: 30 },
  recording: { active: false },
  ...(motion === undefined ? {} : { motion }),
});

const parse = (motion: unknown): ServiceStatus | null => parseStatus(JSON.stringify(status(motion)));

describe('parseStatus motion block', () => {
  it('keeps a valid motion block, including unknown readings', () => {
    expect(parse(MOTION)?.motion).toEqual(MOTION);
    expect(parse(UNKNOWN)?.motion).toEqual(UNKNOWN);
  });

  it('leaves an older status without motion as it was', () => {
    const parsed = parse(undefined);
    expect(parsed).not.toBeNull();
    expect(parsed).not.toHaveProperty('motion');
    expect(parse(null)?.motion).toBeNull();
  });

  it.each([
    ['a negative speed', { ...MOTION, speed_mps: -0.5 }],
    ['an unknown source', { ...MOTION, speed_source: 'radar' }],
    ['a text yaw rate', { ...MOTION, yaw_rate_dps: 'left' }],
    ['a missing fresh flag', { ...MOTION, fresh: undefined }],
    ['a negative age', { ...MOTION, age_ms: -1 }],
    ['an array', [1, 2]],
  ])('keeps the status but drops a motion block with %s', (_, motion) => {
    const parsed = parse(motion);
    expect(parsed).not.toBeNull();
    expect(parsed?.motion).toBeNull();
    expect(parsed?.motion_invalid).toBe(true);
  });

  it('flags only a malformed block as invalid, not a valid, null or absent one', () => {
    expect(parse(MOTION)?.motion_invalid).toBeUndefined();
    expect(parse(null)?.motion_invalid).toBeUndefined();
    expect(parse(undefined)?.motion_invalid).toBeUndefined();
  });
});

describe('motionRows', () => {
  it('shows speed in m/s and mph with its source, then yaw and accelerations', () => {
    expect(motionRows(MOTION)).toEqual([
      { label: 'Speed', value: '1.40 m/s · 3.1 mph from IMU · age 40 ms', level: 'ok' },
      {
        label: 'Motion',
        value: 'yaw 12.5°/s left · accel 0.30 m/s² forward, 0.10 m/s² right',
        level: 'ok',
      },
    ]);
  });

  it('names directions for the other signs and for zero', () => {
    const turning: MotionStatus = { ...MOTION, yaw_rate_dps: -3, accel_fwd_mps2: -0.5, accel_lat_mps2: 0.2 };
    expect(motionRows(turning)[1]?.value).toBe('yaw 3.0°/s right · accel 0.50 m/s² back, 0.20 m/s² left');
    const level: MotionStatus = { ...MOTION, yaw_rate_dps: 0, accel_fwd_mps2: 0, accel_lat_mps2: 0 };
    expect(motionRows(level)[1]?.value).toBe('yaw 0.0°/s · accel 0.00 m/s² forward, 0.00 m/s² sideways');
  });

  it('reads noise that rounds to zero as level, not as a direction that flips', () => {
    const level = 'yaw 0.0°/s · accel 0.00 m/s² forward, 0.00 m/s² sideways';
    const right: MotionStatus = { ...MOTION, yaw_rate_dps: -0.04, accel_fwd_mps2: -0.003, accel_lat_mps2: -0.004 };
    const left: MotionStatus = { ...MOTION, yaw_rate_dps: 0.04, accel_fwd_mps2: 0.003, accel_lat_mps2: 0.004 };
    expect(motionRows(right)[1]?.value).toBe(level);
    expect(motionRows(left)[1]?.value).toBe(level);
    // Just over the rounding edge still has a direction.
    const turning: MotionStatus = { ...MOTION, yaw_rate_dps: -0.05, accel_fwd_mps2: -0.005, accel_lat_mps2: 0.005 };
    expect(motionRows(turning)[1]?.value).toBe('yaw 0.1°/s right · accel 0.01 m/s² back, 0.01 m/s² left');
  });

  it('labels each speed source', () => {
    expect(motionRows({ ...MOTION, speed_source: 'gps' })[0]?.value).toContain('from GPS');
    expect(motionRows({ ...MOTION, speed_mps: 0, speed_source: 'still' })[0]?.value).toBe(
      '0.00 m/s · 0.0 mph (standing still) · age 40 ms',
    );
  });

  it('marks stale motion bad and unknown readings as unknown', () => {
    const rows = motionRows(UNKNOWN);
    expect(rows[0]).toEqual({ label: 'Speed', value: 'Unknown · age —', level: 'bad' });
    expect(rows[1]).toEqual({ label: 'Motion', value: 'yaw — · accel — forward, — sideways', level: 'bad' });
  });

  it('says an older service does not report motion when the block is absent', () => {
    expect(motionRows(undefined)).toEqual([{ label: 'Speed', value: 'Not reported by the service', level: 'none' }]);
  });

  it('says it is waiting for the IMU while the service has no estimate yet', () => {
    expect(motionRows(null)).toEqual([{ label: 'Speed', value: 'Waiting for IMU', level: 'warn' }]);
  });

  it('says the motion data is invalid when the block failed validation', () => {
    expect(motionRows(null, true)).toEqual([{ label: 'Speed', value: 'Invalid motion data', level: 'bad' }]);
  });
});

describe('statusRows with motion', () => {
  it('lists speed and motion after GPS', () => {
    const parsed = parse(MOTION) as ServiceStatus;
    const full: ServiceStatus = {
      ...parsed,
      clock_synced: true,
      sensors_ok: true,
      frame: null,
      gps: null,
      heading: null,
      clock: null,
      stats: { fps: 30, latency_p50_ms: null, latency_p95_ms: null, frames: 0, missed: 0, rejected: 0, rate_limited: 0 },
      camera: null,
      mount: null,
      recording: {
        active: false,
        finishing: false,
        session: null,
        frames: 0,
        sensors: 0,
        dropped: 0,
        error: null,
        end_reason: null,
      },
    };
    const labels = statusRows(full).map((r) => r.label);
    expect(labels.slice(2, 5)).toEqual(['GPS', 'Speed', 'Motion']);
    expect(statusRows({ ...full, motion: undefined }).map((r) => r.label)).toContain('Speed');
    const broken = parseStatus(JSON.stringify({ ...full, motion: { ...MOTION, speed_mps: -1 } })) as ServiceStatus;
    expect(statusRows(broken).find((r) => r.label === 'Speed')?.value).toBe('Invalid motion data');
  });
});
