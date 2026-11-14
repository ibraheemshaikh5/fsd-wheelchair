// Shape of the status the service sends to the viewer and returns from /api/status.
// Mirrors service/drive_service/status.py.

import type { CameraInfo, MountInfo } from './protocol';

export type Level = 'ok' | 'warn' | 'bad' | 'none';

/** One labelled line of the viewer's status list. */
export interface StatusRow {
  readonly label: string;
  readonly value: string;
  readonly level: Level;
}

export interface FrameStatus {
  readonly seq: number;
  readonly w: number;
  readonly h: number;
  readonly age_ms: number | null;
  readonly fresh: boolean;
}

export interface GpsStatus {
  readonly lat: number;
  readonly lon: number;
  readonly accuracy_m: number;
  readonly altitude_m: number | null;
  readonly speed_mps: number | null;
  readonly course_deg: number | null;
  readonly age_ms: number | null;
  readonly fresh: boolean;
}

/** North reference of HeadingStatus.deg: true north once declination is known. */
export type HeadingReference = 'true' | 'magnetic';

/** Where HeadingStatus.deg came from; whether it is true or magnetic is in `reference`. */
export type HeadingSource = 'gps_course' | 'compass';

export interface HeadingStatus {
  /** Best heading in `reference`; only true-north values can be combined with GPS course and maps. */
  readonly deg: number | null;
  readonly source: HeadingSource | null;
  readonly reference: HeadingReference | null;
  /** Magnetic declination applied to the compass, east positive. */
  readonly declination_deg: number | null;
  /** Raw magnetic compass reading. */
  readonly compass_deg: number | null;
  readonly compass_accuracy_deg: number | null;
  readonly compass_age_ms: number | null;
  readonly compass_fresh: boolean;
}

export interface RecordingStatus {
  readonly active: boolean;
  /** Stopped, but the files are still being written; a new start gets 409. */
  readonly finishing: boolean;
  readonly session: string | null;
  readonly frames: number;
  readonly sensors: number;
  readonly dropped: number;
  /** A failure. Reaching a frame, size or disk limit is not an error. */
  readonly error: string | null;
  /** Why the recording ended on its own (a limit was reached), if it did. */
  readonly end_reason: string | null;
}

export interface ServiceStatus {
  readonly type: 'status';
  readonly phone_connected: boolean;
  /** Until the phone clock is synced no reading counts as fresh (fail closed). */
  readonly clock_synced: boolean;
  /** Phone connected, clock synced, and a fresh frame and GPS fix. */
  readonly sensors_ok: boolean;
  readonly frame: FrameStatus | null;
  readonly gps: GpsStatus | null;
  readonly heading: HeadingStatus | null;
  readonly clock: { readonly offset_ms: number; readonly rtt_ms: number; readonly samples: number } | null;
  readonly stats: {
    readonly fps: number;
    readonly latency_p50_ms: number | null;
    readonly latency_p95_ms: number | null;
    readonly frames: number;
    readonly missed: number;
    readonly rejected: number;
    readonly rate_limited: number;
  };
  readonly camera: CameraInfo | null;
  readonly mount: MountInfo | null;
  readonly recording: RecordingStatus;
  /** Where the rider asked to go, measured from the latest fresh fix. Absent from older services. */
  readonly destination?: DestinationStatus | null;
  /** What the service derives from the IMU. Absent from older services; null when unknown or malformed. */
  readonly motion?: MotionStatus | null;
  /**
   * Set by parseStatus (never sent by the service) when the service sent a motion
   * block that failed validation, so a contract break is not mistaken for "no estimate yet".
   */
  readonly motion_invalid?: true;
}

/** Where MotionStatus.speed_mps came from; 'still' means the IMU says the chair is not moving. */
export type SpeedSource = 'gps' | 'imu' | 'still';

const SPEED_SOURCES: readonly SpeedSource[] = ['gps', 'imu', 'still'];

/**
 * The chair's motion from the phone's IMU (fused with GPS). Signs: yaw positive
 * turns left (counter-clockwise from above); accel_fwd positive speeds up along
 * the camera's forward direction; accel_lat positive is toward the chair's left.
 */
export interface MotionStatus {
  readonly speed_mps: number | null;
  readonly speed_source: SpeedSource | null;
  readonly yaw_rate_dps: number | null;
  readonly accel_fwd_mps2: number | null;
  readonly accel_lat_mps2: number | null;
  /** Age of the newest IMU sample used. */
  readonly age_ms: number | null;
  /** age_ms at most 250 ms, after clock sync. */
  readonly fresh: boolean;
}

export interface DestinationStatus {
  readonly lat: number;
  readonly lon: number;
  /** Null while the GPS fix is stale. */
  readonly distance_m: number | null;
  readonly bearing_deg: number | null;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

type Check = (value: unknown) => boolean;

const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const nullOr =
  (check: Check): Check =>
  (value) =>
    value === null || check(value);
const nonNegative: Check = (value) => isFiniteNumber(value) && value >= 0;

const MOTION_FIELDS: Readonly<Record<keyof MotionStatus, Check>> = {
  speed_mps: nullOr(nonNegative),
  speed_source: nullOr((value) => SPEED_SOURCES.includes(value as SpeedSource)),
  yaw_rate_dps: nullOr(isFiniteNumber),
  accel_fwd_mps2: nullOr(isFiniteNumber),
  accel_lat_mps2: nullOr(isFiniteNumber),
  age_ms: nullOr(nonNegative),
  fresh: (value) => typeof value === 'boolean',
};

/** A well-formed motion block, or null: a bad one loses only the motion readings, not the status. */
export function parseMotion(value: unknown): MotionStatus | null {
  if (!isObject(value)) return null;
  const valid = Object.entries(MOTION_FIELDS).every(([key, check]) => check(value[key]));
  return valid ? (value as unknown as MotionStatus) : null;
}

/**
 * A status message from the viewer socket, or null for anything else. Checks
 * only the fields every page relies on; the service is trusted for the rest.
 */
export function parseStatus(raw: string): ServiceStatus | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isObject(value)) return null;
  const valid =
    value.type === 'status' &&
    typeof value.phone_connected === 'boolean' &&
    isObject(value.stats) &&
    isObject(value.recording);
  if (!valid) return null;
  const status = value as unknown as ServiceStatus;
  if (!('motion' in value)) return status;
  const motion = parseMotion(value.motion);
  const invalid = motion === null && value.motion !== null;
  return invalid ? { ...status, motion, motion_invalid: true } : { ...status, motion };
}

/** Envelope used by every /api response. */
export interface ApiResponse<T> {
  readonly success: boolean;
  readonly data: T | null;
  readonly error: string | null;
}
