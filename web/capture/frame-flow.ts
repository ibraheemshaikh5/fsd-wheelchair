// Ack-based flow control for camera frames on one connection.
//
// The send budget (send-budget.ts) only sees the browser's own queue
// (WebSocket.bufferedAmount). On a weak link the backlog sits further down, in
// the OS, TCP and Tailscale, where the page cannot see it, so frames arrived up
// to a second old. The service acks every frame it handles; a frame counts as
// in flight from send until its ack (or any later one) arrives, and the page
// sends a new frame only while few are in flight. That bounds the whole
// backlog, wherever it sits, to a few frames: on a slow link fewer frames go,
// but none of them is old. See docs/phone-protocol.md.

/** Most frames sent but not yet acked. Three keep a link with some latency busy without a backlog. */
export const MAX_FRAMES_IN_FLIGHT = 3;
/** A frame unacked for this long is forgotten, so a lost ack can never stall the stream. */
export const ACK_TIMEOUT_MS = 1000;
/** Weight of each new round-trip sample in the smoothed value. */
const RTT_SMOOTHING = 0.2;
/**
 * Sends remembered for round-trip sampling and ack counting. Unlike the
 * in-flight set they do not expire at ACK_TIMEOUT_MS, so an ack that takes
 * longer than that, on exactly the link this exists for, is still measured.
 * Before the first ack sends are not gated, so this covers a few seconds at 60 fps.
 */
export const RTT_HISTORY = 256;

export interface InFlightFrame {
  readonly seq: number;
  /** Monotonic clock (performance.now) when it was handed to the socket. */
  readonly sentAtMs: number;
}

export interface FrameFlow {
  /** Whether this connection's service has acked a frame; until then it gates nothing (an older service). */
  readonly acking: boolean;
  /** Frames that hold a slot: sent, not acked, and not yet past ACK_TIMEOUT_MS. */
  readonly inFlight: readonly InFlightFrame[];
  /** The last RTT_HISTORY sends on this connection, acked or not. */
  readonly recentSends: readonly InFlightFrame[];
  /** Highest seq acked for a frame this connection sent, or -1. */
  readonly highestAckedSeq: number;
  /** Smoothed send-to-ack round trip, or null before the first sample. */
  readonly rttMs: number | null;
}

export const INITIAL_FLOW: FrameFlow = Object.freeze({
  acking: false,
  inFlight: Object.freeze([]),
  recentSends: Object.freeze([]),
  highestAckedSeq: -1,
  rttMs: null,
});

/** Forget frames that have waited for their ack for ACK_TIMEOUT_MS or longer. */
export function expireFrames(flow: FrameFlow, nowMs: number): FrameFlow {
  const kept = flow.inFlight.filter((frame) => nowMs - frame.sentAtMs < ACK_TIMEOUT_MS);
  return kept.length === flow.inFlight.length ? flow : { ...flow, inFlight: kept };
}

/**
 * Whether one more frame may go out. Only frames actually sent count: one
 * still encoding holds no slot (the send-time check catches it). Before the
 * first ack this always allows: only the send budget applies, as with a
 * service that never acks.
 */
export function maySend(flow: FrameFlow, nowMs: number): boolean {
  if (!flow.acking) return true;
  return expireFrames(flow, nowMs).inFlight.length < MAX_FRAMES_IN_FLIGHT;
}

/** Note a frame just handed to the socket. */
export function recordSent(flow: FrameFlow, seq: number, nowMs: number): FrameFlow {
  const live = expireFrames(flow, nowMs);
  const frame = { seq, sentAtMs: nowMs };
  return {
    ...live,
    inFlight: [...live.inFlight, frame],
    recentSends: [...live.recentSends, frame].slice(-RTT_HISTORY),
  };
}

const sentFrame = (flow: FrameFlow, seq: number): InFlightFrame | undefined =>
  flow.recentSends.find((frame) => frame.seq === seq);

/**
 * Whether an ack is the first for a frame this connection sent (and newer than
 * any acked so far): duplicates, repeats and unknown seqs are not.
 */
export function isNewAck(flow: FrameFlow, seq: number): boolean {
  return seq > flow.highestAckedSeq && sentFrame(flow, seq) !== undefined;
}

const smooth = (previous: number | null, sample: number): number =>
  previous === null ? sample : previous + RTT_SMOOTHING * (sample - previous);

/**
 * Fold in an ack: it frees its frame and every earlier one (their acks may be
 * lost or still coming, but this one shows the link has moved past them). The
 * round trip is sampled from the frame it names, even one that already left
 * the in-flight set, but only once per frame and only for one this connection sent.
 */
export function recordAck(flow: FrameFlow, seq: number, nowMs: number): FrameFlow {
  const inFlight = flow.inFlight.filter((frame) => frame.seq > seq);
  const acked = isNewAck(flow, seq) ? sentFrame(flow, seq) : undefined;
  if (acked === undefined) return { ...flow, acking: true, inFlight };
  return {
    ...flow,
    acking: true,
    inFlight,
    highestAckedSeq: seq,
    rttMs: smooth(flow.rttMs, Math.max(0, nowMs - acked.sentAtMs)),
  };
}
