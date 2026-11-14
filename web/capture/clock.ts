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
export function sampleFromPong(pong: PongMessage, receivedMs: number): ClockSample | null {
  const rtt = receivedMs - pong.t;
  if (rtt < 0) return null;
  return { rtt_ms: rtt, offset_ms: pong.server_t - (pong.t + rtt / 2) };
}
