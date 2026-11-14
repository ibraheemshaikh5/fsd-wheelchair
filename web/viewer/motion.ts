// The viewer's speed and motion rows, from the service's IMU-derived motion block.

import type { Level, MotionStatus, SpeedSource, StatusRow } from '../shared/status';
import { MPS_TO_MPH, formatAge } from './units';

const UNKNOWN = '—';
const SPEED_DIGITS = 2;
const MPH_DIGITS = 1;
const YAW_DIGITS = 1;
const ACCEL_DIGITS = 2;

const SOURCE_NOTES: Readonly<Record<SpeedSource, string>> = {
  gps: 'from GPS',
  imu: 'from IMU',
  still: '(standing still)',
};

/**
 * "0.30 m/s² forward": the size with the direction its sign means, or `zero` when
 * it shows as level. The direction follows the rounded size, so IMU noise around
 * zero reads "0.0" without flipping between left and right. `unit` carries its own spacing.
 */
function signed(value: number | null, digits: number, unit: string, names: readonly [string, string, string]): string {
  const [positive, negative, zero] = names;
  if (value === null) return `${UNKNOWN} ${zero}`.trimEnd();
  const size = Math.abs(value).toFixed(digits);
  const direction = Number(size) === 0 ? zero : value > 0 ? positive : negative;
  return `${size}${unit} ${direction}`.trimEnd();
}

function speedValue(motion: MotionStatus): string {
  const age = `age ${formatAge(motion.age_ms)}`;
  if (motion.speed_mps === null) return `Unknown · ${age}`;
  const mps = motion.speed_mps;
  const speed = `${mps.toFixed(SPEED_DIGITS)} m/s · ${(mps * MPS_TO_MPH).toFixed(MPH_DIGITS)} mph`;
  const source = motion.speed_source === null ? '' : ` ${SOURCE_NOTES[motion.speed_source]}`;
  return `${speed}${source} · ${age}`;
}

function motionValue(motion: MotionStatus): string {
  // Yaw positive is a left (counter-clockwise) turn; lateral positive is toward the left.
  const yaw = signed(motion.yaw_rate_dps, YAW_DIGITS, '°/s', ['left', 'right', '']);
  const forward = signed(motion.accel_fwd_mps2, ACCEL_DIGITS, ' m/s²', ['forward', 'back', 'forward']);
  const lateral = signed(motion.accel_lat_mps2, ACCEL_DIGITS, ' m/s²', ['left', 'right', 'sideways']);
  return `yaw ${yaw} · accel ${forward}, ${lateral}`;
}

/** The single Speed row while there is no motion estimate to show. */
function missingRow(motion: null | undefined, invalid: boolean): StatusRow {
  // Absent: an older service. Null: no estimate yet (before the first IMU batch), or a malformed block.
  if (motion === undefined) return { label: 'Speed', value: 'Not reported by the service', level: 'none' };
  if (invalid) return { label: 'Speed', value: 'Invalid motion data', level: 'bad' };
  return { label: 'Speed', value: 'Waiting for IMU', level: 'warn' };
}

/**
 * Speed (m/s and mph, with its source), then yaw rate and accelerations.
 * `invalid` is ServiceStatus.motion_invalid: the service sent a block that failed validation.
 */
export function motionRows(motion: MotionStatus | null | undefined, invalid = false): readonly StatusRow[] {
  if (motion === null || motion === undefined) return [missingRow(motion, invalid)];
  const level: Level = motion.fresh ? 'ok' : 'bad';
  return [
    { label: 'Speed', value: speedValue(motion), level },
    { label: 'Motion', value: motionValue(motion), level },
  ];
}
