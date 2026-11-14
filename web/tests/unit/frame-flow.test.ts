import { describe, expect, it } from 'vitest';
import {
  ACK_TIMEOUT_MS,
  expireFrames,
  INITIAL_FLOW,
  isNewAck,
  MAX_FRAMES_IN_FLIGHT,
  maySend,
  recordAck,
  recordSent,
  RTT_HISTORY,
  type FrameFlow,
} from '../../capture/frame-flow';

const sendAll = (flow: FrameFlow, seqs: readonly number[], nowMs: number): FrameFlow =>
  seqs.reduce((current, seq) => recordSent(current, seq, nowMs), flow);

describe('frame flow control', () => {
  it('gates nothing before the first ack, as with a service that never acks', () => {
    const flow = sendAll(INITIAL_FLOW, [0, 1, 2, 3, 4, 5], 0);
    expect(flow.acking).toBe(false);
    expect(flow.inFlight).toHaveLength(6);
    expect(maySend(flow, 1)).toBe(true);
  });

  it('allows at most two frames in flight once the service acks', () => {
    // Two cover 20 fps on a round trip up to 100 ms; a third would only add a queued frame.
    expect(MAX_FRAMES_IN_FLIGHT).toBe(2);
    const acked = recordAck(recordSent(INITIAL_FLOW, 0, 0), 0, 30);
    expect(acked).toMatchObject({ acking: true, inFlight: [], rttMs: 30 });
    const one = sendAll(acked, [1], 40);
    // Only frames actually sent count; one still encoding does not hold a slot.
    expect(maySend(one, 40)).toBe(true);
    const two = recordSent(one, 2, 41);
    expect(two.inFlight).toHaveLength(MAX_FRAMES_IN_FLIGHT);
    expect(maySend(two, 41)).toBe(false);
  });

  it('frees a frame and every earlier one on an ack, sampling the round trip from that frame', () => {
    const flow = sendAll(recordAck(INITIAL_FLOW, 0, 0), [1, 2, 3], 100);
    const after = recordAck(flow, 2, 200);
    expect(after.inFlight.map((frame) => frame.seq)).toEqual([3]);
    // Smoothed: the first sample is taken whole, later ones blend in.
    expect(after.rttMs).toBe(100);
    const next = recordAck(recordSent(after, 4, 200), 4, 250);
    expect(next.rttMs).toBeCloseTo(100 + 0.2 * (50 - 100));
  });

  it('keeps the round trip when the acked frame was never sent', () => {
    const flow = recordAck(recordSent(INITIAL_FLOW, 0, 0), 0, 40);
    expect(recordAck(flow, 9, 100).rttMs).toBe(40);
    expect(recordAck(INITIAL_FLOW, 3, 10).rttMs).toBeNull();
  });

  it('samples the round trip from an ack that comes after its frame left the in-flight set', () => {
    // The first ack takes 1.5 s: its frame expired from the gate at 1 s, but the round trip is real.
    const expired = recordSent(recordSent(INITIAL_FLOW, 0, 0), 1, ACK_TIMEOUT_MS + 200);
    expect(expired.inFlight.map((frame) => frame.seq)).toEqual([1]);
    const slow = recordAck(expired, 0, 1500);
    expect(slow.rttMs).toBe(1500);
    // Later slow acks keep pulling the smoothed value up instead of leaving it stale.
    const later = recordAck(recordSent(expireFrames(slow, 4000), 2, 4000), 2, 5600);
    expect(later.rttMs).toBeCloseTo(1500 + 0.2 * (1600 - 1500));
  });

  it('counts an ack once, and only for a frame sent on this connection', () => {
    const flow = sendAll(INITIAL_FLOW, [4, 5, 6], 0);
    expect(isNewAck(flow, 4)).toBe(true);
    expect(isNewAck(flow, 9)).toBe(false);
    const acked = recordAck(flow, 5, 20);
    expect(isNewAck(acked, 5)).toBe(false);
    expect(isNewAck(acked, 4)).toBe(false);
    expect(isNewAck(acked, 6)).toBe(true);
    // An unknown seq still shows the service acks, but samples nothing and is not counted.
    const unknown = recordAck(INITIAL_FLOW, 3, 10);
    expect(unknown).toMatchObject({ acking: true, rttMs: null });
    expect(isNewAck(unknown, 3)).toBe(false);
  });

  it('keeps a bounded history of sends for round-trip sampling', () => {
    const seqs = Array.from({ length: RTT_HISTORY + 10 }, (_, i) => i);
    const flow = sendAll(INITIAL_FLOW, seqs, 0);
    expect(flow.recentSends).toHaveLength(RTT_HISTORY);
    expect(isNewAck(flow, 0)).toBe(false);
    expect(isNewAck(flow, RTT_HISTORY + 9)).toBe(true);
  });

  it('forgets a frame whose ack never came after the timeout, so a lost ack frees its slot', () => {
    const full = sendAll(recordAck(INITIAL_FLOW, 0, 0), [1, 2, 3], 1000);
    expect(maySend(full, 1000 + ACK_TIMEOUT_MS - 1)).toBe(false);
    expect(maySend(full, 1000 + ACK_TIMEOUT_MS)).toBe(true);
    expect(expireFrames(full, 1000 + ACK_TIMEOUT_MS).inFlight).toEqual([]);
    expect(expireFrames(full, 1500)).toBe(full);
  });

  it('drops expired frames when recording a new send', () => {
    const flow = recordSent(recordSent(INITIAL_FLOW, 0, 0), 1, ACK_TIMEOUT_MS);
    expect(flow.inFlight).toEqual([{ seq: 1, sentAtMs: ACK_TIMEOUT_MS }]);
  });

  it('never changes the flow it was given', () => {
    const flow = recordSent(INITIAL_FLOW, 0, 0);
    recordAck(flow, 0, 10);
    recordSent(flow, 1, 10);
    expect(flow.inFlight).toEqual([{ seq: 0, sentAtMs: 0 }]);
    expect(INITIAL_FLOW.inFlight).toEqual([]);
  });
});
