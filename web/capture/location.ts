import { validNonNegative } from '../shared/angles';
import { type GpsMessage, PROTOCOL_VERSION } from '../shared/protocol';

export interface CoordinatesLike {
  readonly latitude: number;
  readonly longitude: number;
  readonly accuracy: number;
  readonly altitude: number | null;
  readonly speed: number | null;
  readonly heading: number | null;
}

export interface PositionLike {
  readonly coords: CoordinatesLike;
  readonly timestamp: number;
}

const FULL_TURN_DEG = 360;

// Safari reports speed and heading as null, NaN or -1 when it has no value.
const nonNegativeOrNull = (value: number | null): number | null => (validNonNegative(value) ? value : null);

/**
 * The last fix, ready to send again after a reconnect, or null if it is too old.
 * Browsers often deliver the first fix before the socket is open, and a phone
 * standing still may not report again for a while. The service ages a fix by
 * its fix_t (not by when it arrives), so a resend never makes it look newer;
 * a fix too old to be of use is simply not resent.
 */
export function recentFix(fix: GpsMessage | null, nowMs: number, maxAgeMs: number): GpsMessage | null {
  if (fix === null || nowMs - fix.fix_t > maxAgeMs) return null;
  return { ...fix, t: nowMs };
}

export function toGpsMessage(position: PositionLike, nowMs: number): GpsMessage {
  const { coords } = position;
  const course = nonNegativeOrNull(coords.heading);
  return {
    v: PROTOCOL_VERSION,
    type: 'gps',
    t: nowMs,
    lat: coords.latitude,
    lon: coords.longitude,
    accuracy_m: coords.accuracy,
    altitude_m: coords.altitude !== null && Number.isFinite(coords.altitude) ? coords.altitude : null,
    speed_mps: nonNegativeOrNull(coords.speed),
    course_deg: course === null ? null : course % FULL_TURN_DEG,
    fix_t: position.timestamp > 0 ? position.timestamp : nowMs,
  };
}
