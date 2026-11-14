// How much the phone lets queue up in its WebSocket before it skips frames.
//
// The queue only fills when the Wi-Fi uplink is slower than the camera, so how
// long a message waits in it is the queued bytes divided by the link's real
// throughput, not by the camera's rate. GPS and IMU messages are never dropped
// and go out on the same socket, so they wait behind whatever frames are queued;
// a long queue makes the iPad's speed and motion readings late. The limit is
// therefore set in time: frames are skipped once the queue would take longer
// than the budget to drain at the measured link rate. Skipping frames on a slow
// link is the right trade; the model only takes 20 fps of them.

/** The queue limit: a time budget, and a byte cap used before the link is measured. */
export interface SendBudget {
  /** Longest a newly queued message should wait behind the queue ahead of it. */
  readonly budgetMs: number;
  /** Most bytes ever queued; also the limit until the link has been measured. */
  readonly maxBytes: number;
}

// 100 ms leaves the IMU's 50 ms batching plus this wait well inside the
// service's 250 ms motion freshness limit. 100 KB is about two 640 px JPEGs.
export const SEND_BUDGET: SendBudget = Object.freeze({ budgetMs: 100, maxBytes: 100_000 });

/** What the transport has learned about the uplink from the socket's queue. */
export interface LinkEstimate {
  /** When bufferedAmount was last read, or null before the first read. */
  readonly atMs: number | null;
  /** Bytes queued at that read plus everything sent since. */
  readonly queuedBytes: number;
  /** Bytes drained, and time spent, while the queue stayed busy, not yet judged. */
  readonly windowBytes: number;
  readonly windowMs: number;
  /** Measured uplink rate, or null until the queue has stayed busy long enough to tell. */
  readonly bytesPerMs: number | null;
}

export const NO_ESTIMATE: LinkEstimate = Object.freeze({
  atMs: null,
  queuedBytes: 0,
  windowBytes: 0,
  windowMs: 0,
  bytesPerMs: null,
});

/**
 * Busy time to watch before trusting a rate. Safari only lowers bufferedAmount
 * when a whole message has gone, so a shorter window would see a link that
 * alternates between stalled and instant.
 */
const RATE_WINDOW_MS = 200;

/** Fold in a fresh read of the socket's bufferedAmount. */
export function observeQueue(estimate: LinkEstimate, nowMs: number, bufferedAmount: number): LinkEstimate {
  if (estimate.atMs === null || nowMs <= estimate.atMs) {
    return { ...estimate, atMs: estimate.atMs ?? nowMs, queuedBytes: bufferedAmount };
  }
  const elapsed = nowMs - estimate.atMs;
  const drained = Math.max(0, estimate.queuedBytes - bufferedAmount);
  const moved = { ...estimate, atMs: nowMs, queuedBytes: bufferedAmount };
  // An interval that began with nothing queued says nothing about the link. One
  // that ended empty still counts, whole: Safari reports a message's bytes as
  // gone all at once when it finishes, so they really drained over the earlier
  // intervals too. Counting its idle tail only underestimates, the safe side.
  if (estimate.queuedBytes === 0) return moved;
  const windowBytes = estimate.windowBytes + drained;
  const windowMs = estimate.windowMs + elapsed;
  if (windowMs < RATE_WINDOW_MS) return { ...moved, windowBytes, windowMs };
  return { ...moved, windowBytes: 0, windowMs: 0, bytesPerMs: windowBytes / windowMs };
}

/** Note bytes just handed to the socket. */
export function recordSend(estimate: LinkEstimate, bytes: number): LinkEstimate {
  return { ...estimate, queuedBytes: estimate.queuedBytes + bytes };
}

/** Most bytes that may sit in the queue: what the link drains within the budget, capped. */
export function queueLimitBytes(estimate: LinkEstimate, budget: SendBudget): number {
  if (estimate.bytesPerMs === null) return budget.maxBytes;
  return Math.min(budget.maxBytes, estimate.bytesPerMs * budget.budgetMs);
}

/**
 * Whether a droppable message of `bytes` may be queued. An empty queue always
 * takes one, so even a link slower than one frame per budget still gets frames.
 */
export function fitsBudget(bufferedAmount: number, bytes: number, limitBytes: number): boolean {
  return bufferedAmount === 0 || bufferedAmount + bytes <= limitBytes;
}
