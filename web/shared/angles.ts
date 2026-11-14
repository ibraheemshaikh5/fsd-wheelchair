// Small pure helpers for compass angles and browser sensor values.

const FULL_TURN_DEG = 360;
const RAD_PER_DEG = Math.PI / 180;

/** Below this mean resultant length the samples point every way and have no mean direction. */
const MIN_RESULTANT = 1e-9;

/**
 * True for a finite value >= 0. Safari reports an unknown GPS course or speed
 * as null, NaN or -1, which must never be averaged in.
 */
export function validNonNegative(value: number | null): value is number {
  return value !== null && Number.isFinite(value) && value >= 0;
}

/** Wrap any angle into [0, 360). */
export function normaliseDeg(deg: number): number {
  const wrapped = ((deg % FULL_TURN_DEG) + FULL_TURN_DEG) % FULL_TURN_DEG;
  return wrapped >= FULL_TURN_DEG ? 0 : wrapped;
}

/**
 * Mean direction of compass angles in degrees, in [0, 360): the mean of
 * 358° and 2° is 0°, not 180°. Null when there are no samples or they cancel out.
 */
export function circularMeanDeg(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sin = values.reduce((sum, deg) => sum + Math.sin(deg * RAD_PER_DEG), 0) / values.length;
  const cos = values.reduce((sum, deg) => sum + Math.cos(deg * RAD_PER_DEG), 0) / values.length;
  if (Math.hypot(sin, cos) < MIN_RESULTANT) return null;
  return normaliseDeg(Math.atan2(sin, cos) / RAD_PER_DEG);
}
