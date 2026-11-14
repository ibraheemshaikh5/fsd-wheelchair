import { mpsToMph } from '../../lib/format'

// The service reports some readings far more finely than the iPad shows them,
// and they change on nearly every status (about 30 a second): the frame rate is a
// float over a sliding window, IMU speed moves with every batch, the compass
// wobbles. Rounding them to what the screen shows lets an unchanged screen skip
// re-rendering the whole app.

/** The speed arc shows tenths of a mile per hour. */
const SPEED_STEP_MPH = 0.1
const SPEED_STEP_MPS = SPEED_STEP_MPH / mpsToMph(1)
const FULL_TURN_DEG = 360

/** Speed to the nearest 0.1 mph, in m/s. */
export function displaySpeedMps(mps: number): number {
  return Math.round(mps / SPEED_STEP_MPS) * SPEED_STEP_MPS
}

/** Whole degrees in 0–359: 359.7 is 0, not 360. */
export function displayHeadingDeg(deg: number): number {
  return ((Math.round(deg) % FULL_TURN_DEG) + FULL_TURN_DEG) % FULL_TURN_DEG
}

/** Whole frames a second. */
export function displayFps(fps: number): number {
  return Math.round(fps)
}
