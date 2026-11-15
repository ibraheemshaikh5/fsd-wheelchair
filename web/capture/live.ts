// What the capture page shows about one streaming session, and how it is shown.

import type { Level } from '../shared/status';
import type { ClockSample } from './clock';
import type { LinkView } from './link-stats';
import { describeConnection } from './status';
import type { TransportState } from './transport';

export type MotionAccess = 'granted' | 'denied' | 'not-needed';

export interface Counters {
  readonly captured: number;
  readonly sent: number;
  readonly skipped: number;
}

export interface LiveState {
  readonly connection: TransportState;
  readonly counters: Counters;
  readonly cameraLabel: string | null;
  readonly gpsAccuracyM: number | null;
  readonly headingDeg: number | null;
  readonly motion: MotionAccess | null;
  readonly wakeLock: boolean | null;
  readonly clock: ClockSample | null;
  /** How the frame stream copes with the link, refreshed once a second; null until then. */
  readonly link: LinkView | null;
}

export const INITIAL_LIVE: LiveState = Object.freeze({
  connection: Object.freeze({ status: 'stopped' as const, attempt: 0, lastClose: null }),
  counters: Object.freeze({ captured: 0, sent: 0, skipped: 0 }),
  cameraLabel: null,
  gpsAccuracyM: null,
  headingDeg: null,
  motion: null,
  wakeLock: null,
  clock: null,
  link: null,
});

/**
 * What to show once a session has ended: nothing live, so an old fix or
 * camera never looks current, but keep why the link closed (e.g. a takeover).
 */
export function stoppedLive(last: LiveState): LiveState {
  return {
    ...INITIAL_LIVE,
    connection: { status: 'stopped', attempt: 0, lastClose: last.connection.lastClose },
  };
}

export interface StatusRow {
  readonly label: string;
  readonly value: string;
  readonly level: Level;
}

const MOTION_TEXT: Record<MotionAccess, string> = {
  granted: 'Allowed',
  denied: 'Denied: no compass or IMU',
  'not-needed': 'Available',
};

function wakeLockRow(held: boolean | null): StatusRow {
  if (held === null) return { label: 'Screen lock', value: '—', level: 'none' };
  return held
    ? { label: 'Screen lock', value: 'Kept on', level: 'none' }
    : { label: 'Screen lock', value: 'Not held: set Auto-Lock to Never', level: 'warn' };
}

/** Sent below this share of the captured rate, the link is the bottleneck. */
const WEAK_SEND_SHARE = 0.7;
/** Frames acked slower than this are too old by arrival for driving. */
const SLOW_ACK_MS = 150;

const fps = (value: number): string => `${Math.round(value)}`;

const ackText = (link: LinkView): string =>
  link.ackRttMs === null ? 'Waiting for first ack' : `${Math.round(link.ackRttMs)} ms`;

/** Sent vs captured rate, flow-control skips, JPEG quality and ack round trip, so a weak link shows. */
export function linkStatusRows(link: LinkView | null): readonly StatusRow[] {
  if (link === null) {
    return ['Send rate', 'Held back', 'JPEG quality', 'Frame ack'].map((label) => ({
      label,
      value: '—',
      level: 'none' as const,
    }));
  }
  const weak = link.sentFps < WEAK_SEND_SHARE * link.capturedFps;
  const slowAck = link.ackRttMs !== null && link.ackRttMs > SLOW_ACK_MS;
  return [
    { label: 'Send rate', value: `${fps(link.sentFps)} of ${fps(link.capturedFps)} fps`, level: weak ? 'warn' : 'ok' },
    {
      label: 'Held back',
      value: `${fps(link.flowSkipsPerS)}/s by slow link (${link.flowSkips} total)`,
      level: link.flowSkipsPerS > 0 ? 'warn' : 'none',
    },
    { label: 'JPEG quality', value: link.jpegQuality.toFixed(2), level: 'none' },
    {
      label: 'Frame ack',
      value: ackText(link),
      level: slowAck ? 'warn' : 'none',
    },
  ];
}

export function captureStatusRows(live: LiveState): readonly StatusRow[] {
  const connection = describeConnection(live.connection);
  const { captured, sent, skipped } = live.counters;
  const gps = live.gpsAccuracyM;
  return [
    { label: 'Service', value: connection.text, level: connection.level },
    { label: 'Camera', value: live.cameraLabel ?? '—', level: live.cameraLabel ? 'ok' : 'none' },
    { label: 'Frames', value: `${sent} sent · ${skipped} skipped · ${captured} captured`, level: 'none' },
    { label: 'GPS', value: gps === null ? 'Waiting for fix' : `±${gps.toFixed(1)} m`, level: gps === null ? 'warn' : 'ok' },
    { label: 'Compass', value: live.headingDeg === null ? '—' : `${Math.round(live.headingDeg)}°`, level: 'none' },
    {
      label: 'Motion',
      value: live.motion === null ? '—' : MOTION_TEXT[live.motion],
      level: live.motion === 'denied' ? 'bad' : 'none',
    },
    wakeLockRow(live.wakeLock),
    { label: 'Round trip', value: live.clock === null ? '—' : `${Math.round(live.clock.rtt_ms)} ms`, level: 'none' },
    ...linkStatusRows(live.link),
  ];
}
