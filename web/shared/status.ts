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
  return valid ? (value as unknown as ServiceStatus) : null;
}

/** Envelope used by every /api response. */
export interface ApiResponse<T> {
  readonly success: boolean;
  readonly data: T | null;
  readonly error: string | null;
}
