// Per-connection frame counters, the once-a-second `link` message built from
// them, and the rates the phone's own status panel shows.

import { type LinkMessage, PROTOCOL_VERSION } from '../shared/protocol';

export interface LinkCounters {
  readonly captured: number;
  readonly sent: number;
  readonly acked: number;
  readonly skipped_flow: number;
  readonly skipped_encoder: number;
  readonly skipped_socket: number;
}

export type LinkCounter = keyof LinkCounters;
/** Why a captured frame was not sent. */
export type SkipReason = 'flow' | 'encoder' | 'socket';

export const ZERO_COUNTERS: LinkCounters = Object.freeze({
  captured: 0,
  sent: 0,
  acked: 0,
  skipped_flow: 0,
  skipped_encoder: 0,
  skipped_socket: 0,
});

const SKIP_COUNTER: Record<SkipReason, LinkCounter> = {
  flow: 'skipped_flow',
  encoder: 'skipped_encoder',
  socket: 'skipped_socket',
};

export const bump = (counters: LinkCounters, key: LinkCounter): LinkCounters => ({
  ...counters,
  [key]: counters[key] + 1,
});

export const countSkip = (counters: LinkCounters, reason: SkipReason): LinkCounters =>
  bump(counters, SKIP_COUNTER[reason]);

/** Frames the link itself held back: what adaptive quality reacts to. */
export const linkSkips = (counters: LinkCounters): number => counters.skipped_flow + counters.skipped_socket;

/** Counters gained since `before`. */
export const counterDelta = (after: LinkCounters, before: LinkCounters): LinkCounters => ({
  captured: after.captured - before.captured,
  sent: after.sent - before.sent,
  acked: after.acked - before.acked,
  skipped_flow: after.skipped_flow - before.skipped_flow,
  skipped_encoder: after.skipped_encoder - before.skipped_encoder,
  skipped_socket: after.skipped_socket - before.skipped_socket,
});

export interface LinkSnapshot {
  readonly counters: LinkCounters;
  readonly inFlight: number;
  readonly jpegQuality: number;
  readonly ackRttMs: number | null;
}

const oneDecimal = (value: number): number => Math.round(value * 10) / 10;

export function buildLinkMessage(snapshot: LinkSnapshot, wallNowMs: number): LinkMessage {
  return {
    v: PROTOCOL_VERSION,
    type: 'link',
    t: wallNowMs,
    ...snapshot.counters,
    in_flight: snapshot.inFlight,
    jpeg_quality: snapshot.jpegQuality,
    ack_rtt_ms: snapshot.ackRttMs === null ? null : oneDecimal(snapshot.ackRttMs),
  };
}

/**
 * Whether the service acks frames: 'acking' once it has on this connection,
 * 'waiting' while an ack may still come (none sent yet, the first is under
 * ACK_TIMEOUT_MS old, or the service acked on an earlier connection), and
 * 'none' once a frame has gone unacked past that (an older service).
 */
export type AckStatus = 'acking' | 'waiting' | 'none';

/** What the phone's status panel shows about the link, refreshed once a second. */
export interface LinkView {
  readonly capturedFps: number;
  readonly sentFps: number;
  /** Frames held back by flow control in the last window, per second. */
  readonly flowSkipsPerS: number;
  /** Frames held back by flow control since this connection opened. */
  readonly flowSkips: number;
  readonly jpegQuality: number;
  readonly ackRttMs: number | null;
  /** Flow control applies only while 'acking'. */
  readonly ackStatus: AckStatus;
}

const MS_PER_SECOND = 1000;

/** Rates over one window of `elapsedMs`. */
export function linkView(
  window: LinkCounters,
  elapsedMs: number,
  snapshot: LinkSnapshot,
  ackStatus: AckStatus,
): LinkView {
  const perSecond = (count: number): number => (elapsedMs > 0 ? (count * MS_PER_SECOND) / elapsedMs : 0);
  return {
    capturedFps: perSecond(window.captured),
    sentFps: perSecond(window.sent),
    flowSkipsPerS: perSecond(window.skipped_flow),
    flowSkips: snapshot.counters.skipped_flow,
    jpegQuality: snapshot.jpegQuality,
    ackRttMs: snapshot.ackRttMs,
    ackStatus,
  };
}
