import type { PongMessage } from '../shared/protocol';

export interface ClockSample {
  readonly rtt_ms: number;
  /** server_time = phone_time + offset_ms */
  readonly offset_ms: number;
}

/**
 * Measure round trip and clock offset from one ping/pong exchange, assuming the
 * network delay is the same in both directions. The service keeps the sample
 * with the shortest round trip, which is the least distorted.
 */
/**
 * Turn a monotonic-clock time (performance.now, event.timeStamp, video frame
 * callback times) into ms since the Unix epoch on the wall clock (Date.now) the
 * clock-offset pings use.
 *
 * timeOrigin + performance.now() may drift from Date.now(): on iOS the monotonic
 * clock stops while the phone sleeps, and the wall clock can be stepped by NTP.
 * So only the short delay since the event (measured on the monotonic clock) is
 * taken from it, and subtracted from the wall clock now. A time in the future
 * counts as no delay, so nothing is stamped later than now. A delay longer than
 * maxDelayMs means the time is not on this clock at all, so now is used.
 */
export function monotonicToWallMs(
  monotonicMs: number | undefined,
  wallNowMs: number,
  monotonicNowMs: number,
  maxDelayMs = Number.POSITIVE_INFINITY,
): number {
  if (monotonicMs === undefined || !Number.isFinite(monotonicMs)) return wallNowMs;
  const delay = Math.max(0, monotonicNowMs - monotonicMs);
  return delay > maxDelayMs ? wallNowMs : wallNowMs - delay;
}

export function sampleFromPong(pong: PongMessage, receivedMs: number): ClockSample | null {
  const rtt = receivedMs - pong.t;
  if (rtt < 0) return null;
  return { rtt_ms: rtt, offset_ms: pong.server_t - (pong.t + rtt / 2) };
}
