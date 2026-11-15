import { type HeadingMessage, isFiniteNumber, PROTOCOL_VERSION } from '../shared/protocol';

/** The parts of a DeviceOrientationEvent this uses, including Safari's compass fields. */
export interface OrientationLike {
  readonly webkitCompassHeading?: number;
  readonly webkitCompassAccuracy?: number;
  readonly alpha: number | null;
  readonly absolute: boolean;
}

const FULL_TURN_DEG = 360;

/**
 * Turn an orientation event into a compass heading.
 *
 * iOS Safari gives `webkitCompassHeading` directly (clockwise from magnetic
 * north). Other browsers give an absolute `alpha`, which runs counter-clockwise.
 * A relative alpha is useless as a heading.
 */
export function toHeadingMessage(event: OrientationLike, nowMs: number): HeadingMessage | null {
  if (isFiniteNumber(event.webkitCompassHeading) && event.webkitCompassHeading >= 0) {
    const accuracy = event.webkitCompassAccuracy;
    return {
      v: PROTOCOL_VERSION,
      type: 'heading',
      t: nowMs,
      magnetic_deg: event.webkitCompassHeading % FULL_TURN_DEG,
      // iOS reports -1 when the compass needs calibrating.
      accuracy_deg: isFiniteNumber(accuracy) && accuracy >= 0 ? accuracy : null,
    };
  }
  if (event.absolute && isFiniteNumber(event.alpha)) {
    return {
      v: PROTOCOL_VERSION,
      type: 'heading',
      t: nowMs,
      magnetic_deg: (FULL_TURN_DEG - event.alpha) % FULL_TURN_DEG,
      accuracy_deg: null,
    };
  }
  return null;
}
