import type { FrameStatus, HeadingStatus, Level, RecordingStatus, ServiceStatus, StatusRow } from '../shared/status';

/** Above this, the stream is too slow to drive on at walking pace. */
const LATENCY_BAD_MS = 500;
const LATENCY_WARN_MS = 250;
const MS_PER_S = 1000;

export function formatAge(ms: number | null): string {
  if (ms === null) return '—';
  return ms < MS_PER_S ? `${Math.round(ms)} ms` : `${(ms / MS_PER_S).toFixed(1)} s`;
}

const ms = (value: number): string => `${Math.round(value)} ms`;

const FULL_TURN_DEG = 360;

/** Whole degrees in 0–359; 359.6 must read 0°, not 360°. */
export function formatBearing(deg: number): string {
  return `${Math.round(deg) % FULL_TURN_DEG}°`;
}

/** Fail closed: a status without the flag (an older service) counts as not synced. */
const clockSynced = (status: ServiceStatus): boolean => status.clock_synced === true;

function phoneRow(status: ServiceStatus): StatusRow {
  if (!status.phone_connected) return { label: 'Phone', value: 'Not connected', level: 'bad' };
  if (!clockSynced(status)) return { label: 'Phone', value: 'Connected, waiting for clock sync', level: 'warn' };
  return status.sensors_ok
    ? { label: 'Phone', value: 'Connected, sensors OK', level: 'ok' }
    : { label: 'Phone', value: 'Connected, sensors stale', level: 'warn' };
}

function cameraRow(status: ServiceStatus): StatusRow {
  const frame = status.frame;
  if (frame === null) return { label: 'Camera', value: 'No frames yet', level: 'bad' };
  const parts = [
    ...(status.camera ? [status.camera.label] : []),
    `${frame.w}×${frame.h}`,
    `${status.stats.fps.toFixed(1)} fps`,
    `age ${formatAge(frame.age_ms)}`,
  ];
  return { label: 'Camera', value: parts.join(' · '), level: frame.fresh ? 'ok' : 'bad' };
}

function gpsRow(status: ServiceStatus): StatusRow {
  const gps = status.gps;
  if (gps === null) return { label: 'GPS', value: 'No fix yet', level: 'bad' };
  const parts = [
    `${gps.lat.toFixed(6)}, ${gps.lon.toFixed(6)}`,
    `±${gps.accuracy_m.toFixed(1)} m`,
    ...(gps.speed_mps === null ? [] : [`${gps.speed_mps.toFixed(1)} m/s`]),
    `age ${formatAge(gps.age_ms)}`,
  ];
  return { label: 'GPS', value: parts.join(' · '), level: gps.fresh ? 'ok' : 'bad' };
}

function compassNote(heading: HeadingStatus): string {
  if (heading.compass_deg === null) return '';
  const accuracy =
    heading.compass_accuracy_deg === null ? ', uncalibrated' : ` ±${Math.round(heading.compass_accuracy_deg)}°`;
  const stale = heading.compass_fresh ? '' : ', stale';
  return ` (compass ${formatBearing(heading.compass_deg)}${accuracy}${stale})`;
}

const SOURCE_LABELS: Readonly<Record<NonNullable<HeadingStatus['source']>, string>> = {
  gps_course: 'GPS course',
  compass: 'compass',
};

/** East-positive declination as a compass direction: -11 reads "11.0° W". */
export function formatDeclination(deg: number): string {
  const size = `${Math.abs(deg).toFixed(1)}°`;
  if (deg > 0) return `${size} E`;
  return deg < 0 ? `${size} W` : size;
}

function referenceLabel(heading: HeadingStatus): string {
  return heading.reference === null || heading.reference === undefined ? '(reference unknown)' : heading.reference;
}

function declinationNote(heading: HeadingStatus): string {
  if (heading.declination_deg !== null && heading.declination_deg !== undefined) {
    return ` · declination ${formatDeclination(heading.declination_deg)}`;
  }
  return heading.reference === 'magnetic' ? ' · declination unknown' : '';
}

function headingRow(status: ServiceStatus): StatusRow {
  const heading = status.heading;
  if (heading === null) return { label: 'Heading', value: 'None', level: 'warn' };
  if (heading.deg === null) return { label: 'Heading', value: `None${compassNote(heading)}`, level: 'warn' };
  const source = heading.source === null ? 'unknown source' : SOURCE_LABELS[heading.source];
  const value = `${formatBearing(heading.deg)} ${referenceLabel(heading)} from ${source}${compassNote(heading)}${declinationNote(heading)}`;
  // Only a true-north heading lines up with GPS course and the map.
  return { label: 'Heading', value, level: heading.reference === 'true' ? 'ok' : 'warn' };
}

function latencyRow(status: ServiceStatus): StatusRow {
  const { latency_p50_ms: p50, latency_p95_ms: p95 } = status.stats;
  if (!clockSynced(status) || status.clock === null || p50 === null || p95 === null) {
    return { label: 'Latency', value: 'Waiting for clock sync', level: 'warn' };
  }
  const level: Level = p95 >= LATENCY_BAD_MS ? 'bad' : p95 >= LATENCY_WARN_MS ? 'warn' : 'ok';
  return { label: 'Latency', value: `p50 ${ms(p50)} · p95 ${ms(p95)} · RTT ${ms(status.clock.rtt_ms)}`, level };
}

function framesRow(status: ServiceStatus): StatusRow {
  const s = status.stats;
  const value = `${s.frames} received · ${s.missed} missed · ${s.rejected} rejected · ${s.rate_limited} rate-limited`;
  return { label: 'Frames', value, level: s.rejected > 0 ? 'warn' : 'none' };
}

function recordingState(r: RecordingStatus): readonly string[] {
  if (r.active) return [];
  return r.finishing ? ['finishing…'] : ['stopped'];
}

function recordingRow(status: ServiceStatus): StatusRow {
  const r = status.recording;
  const endReason = r.end_reason ?? null;
  if (!r.active && !r.finishing && r.session === null && r.error === null && endReason === null) {
    return { label: 'Recording', value: 'Off', level: 'none' };
  }
  const parts = [
    r.session ?? 'unnamed',
    `${r.frames} frames`,
    `${r.sensors} sensor lines`,
    `${r.dropped} dropped`,
    ...(r.error === null ? [] : [r.error]),
    ...(endReason === null ? [] : [`ended: ${endReason}`]),
    ...recordingState(r),
  ];
  // Reaching a limit is a clean end; only an error is a failure.
  const level: Level = r.error !== null ? 'bad' : r.active ? 'ok' : 'none';
  return { label: 'Recording', value: parts.join(' · '), level };
}

export function statusRows(status: ServiceStatus): readonly StatusRow[] {
  return [
    phoneRow(status),
    cameraRow(status),
    gpsRow(status),
    headingRow(status),
    latencyRow(status),
    framesRow(status),
    recordingRow(status),
  ];
}

/**
 * Text over the camera frame. Once the viewer has lost the service, the last
 * age it heard is frozen, so it is not shown as if it were current.
 */
export function frameBadge(frame: FrameStatus | null, connected: boolean): string {
  const lost = connected ? '' : ' · DISCONNECTED';
  if (frame === null) return `No frames yet${lost}`;
  if (!connected) return `#${frame.seq}${lost}`;
  return `#${frame.seq} · ${formatAge(frame.age_ms)} old${frame.fresh ? '' : ' · STALE'}`;
}

interface RecordButtonState {
  readonly label: string;
  readonly className: 'primary' | 'danger';
  readonly disabled: boolean;
}

interface RecordButtonContext {
  /** The viewer socket is open. */
  readonly connected: boolean;
  /** A start or stop request is in flight. */
  readonly busy: boolean;
}

export function recordButtonState(recording: RecordingStatus | null, context: RecordButtonContext): RecordButtonState {
  const blocked = recording === null || !context.connected || context.busy;
  if (recording?.active === true) return { label: 'Stop recording', className: 'danger', disabled: blocked };
  if (recording?.finishing === true) return { label: 'Finishing recording…', className: 'primary', disabled: true };
  return { label: 'Start recording', className: 'primary', disabled: blocked };
}
