// Turn camera frames so they come out upright, and landscape when the phone is
// mounted sideways.
//
// Safari cannot lock the page to landscape. With Portrait Orientation Lock on
// (or the page held in portrait), a sideways phone still delivers portrait video
// whose "up" is the phone's top edge, so the picture is on its side. Gravity
// from devicemotion says which way the phone is really turned; the frame is
// rotated by that quarter turn before it is encoded. When Safari has already
// turned the page and video to landscape, the frame is left alone.

/** Clockwise degrees to turn a camera frame. */
export type Rotation = 0 | 90 | 180 | 270;
/** The rotation setting: AUTO_ROTATION follows gravity; otherwise a fixed quarter turn. */
export type RotationSetting = typeof AUTO_ROTATION | Rotation;

export const AUTO_ROTATION = -1;
export const ROTATION_SETTINGS: readonly RotationSetting[] = [AUTO_ROTATION, 0, 90, 180, 270];

/** Smoothing time constant for gravity, so a jolt while walking does not flip the picture. */
const GRAVITY_TAU_MS = 500;
/** Under this much gravity in the screen's plane the phone is lying flat: keep the last rotation. */
const FLAT_MPS2 = 4;
/** The current rotation is kept until the phone is turned this far from it (45 would flip on every wobble). */
const KEEP_WITHIN_DEG = 60;
const QUARTER = 90;
const FULL_TURN = 360;

/**
 * Smoothed gravity in the screen's plane, device axes (x right, y toward the top).
 * On iOS accelerationIncludingGravity is the gravity vector itself, pointing down,
 * so an upright phone reads y = -9.8 (the opposite of the W3C spec's sign).
 */
export interface Gravity {
  readonly x: number;
  readonly y: number;
  readonly known: boolean;
}

export const INITIAL_GRAVITY: Gravity = Object.freeze({ x: 0, y: 0, known: false });

export function updateGravity(gravity: Gravity, x: number, y: number, dtMs: number): Gravity {
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(dtMs) || dtMs < 0) return gravity;
  if (!gravity.known) return { x, y, known: true };
  const weight = 1 - Math.exp(-dtMs / GRAVITY_TAU_MS);
  return { x: gravity.x + weight * (x - gravity.x), y: gravity.y + weight * (y - gravity.y), known: true };
}

const wrap = (degrees: number): number => ((degrees % FULL_TURN) + FULL_TURN) % FULL_TURN;
const separation = (a: number, b: number): number => {
  const d = wrap(a - b);
  return Math.min(d, FULL_TURN - d);
};

/**
 * The clockwise quarter turn that puts the real world's up at the top of a frame
 * whose up is the phone's top edge. Gravity pointing toward the phone's bottom
 * edge (-y) is 0; toward its right edge (+x, phone turned clockwise) is 90.
 */
export function physicalRotation(gravity: Gravity, previous: Rotation): Rotation {
  if (!gravity.known || Math.hypot(gravity.x, gravity.y) < FLAT_MPS2) return previous;
  const angle = wrap((Math.atan2(gravity.x, -gravity.y) * 180) / Math.PI);
  if (separation(angle, previous) <= KEEP_WITHIN_DEG) return previous;
  return wrap(Math.round(angle / QUARTER) * QUARTER) as Rotation;
}

/** The rotation to apply to a frame from a video of this size. */
export function frameRotation(
  videoWidth: number,
  videoHeight: number,
  physical: Rotation,
  setting: RotationSetting,
): Rotation {
  if (setting !== AUTO_ROTATION) return setting;
  // Already landscape: Safari turned the page and video with the phone.
  if (videoWidth > videoHeight) return 0;
  return physical;
}

export function rotatedSize(w: number, h: number, rotation: Rotation): { w: number; h: number } {
  return rotation === 90 || rotation === 270 ? { w: h, h: w } : { w, h };
}

/** Canvas setTransform(a, b, c, d, e, f) that draws a w x h image turned clockwise by `rotation`. */
export function drawTransform(rotation: Rotation, w: number, h: number): readonly number[] {
  switch (rotation) {
    case 90:
      return [0, 1, -1, 0, h, 0];
    case 180:
      return [-1, 0, 0, -1, w, h];
    case 270:
      return [0, -1, 1, 0, 0, w];
    default:
      return [1, 0, 0, 1, 0, 0];
  }
}

/** What the encoder needs to turn a frame: how the phone is held, and the setting. */
export interface FrameOrientation {
  readonly physical: Rotation;
  readonly setting: RotationSetting;
}

/** Follows gravity from devicemotion events (times on the monotonic clock). */
export class OrientationTracker {
  private gravity: Gravity = INITIAL_GRAVITY;
  private lastMs: number | null = null;
  private rotation: Rotation = 0;

  observe(reading: { x: number | null; y: number | null } | null, monotonicMs: number): void {
    if (reading === null || reading.x === null || reading.y === null) return;
    const dtMs = this.lastMs === null ? 0 : monotonicMs - this.lastMs;
    this.lastMs = monotonicMs;
    this.gravity = updateGravity(this.gravity, reading.x, reading.y, dtMs);
    this.rotation = physicalRotation(this.gravity, this.rotation);
  }

  get physical(): Rotation {
    return this.rotation;
  }
}
