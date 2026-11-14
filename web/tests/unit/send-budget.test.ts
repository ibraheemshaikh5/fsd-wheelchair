import { describe, expect, it } from 'vitest';
import {
  fitsBudget,
  NO_ESTIMATE,
  observeQueue,
  queueLimitBytes,
  recordSend,
  SEND_BUDGET,
  type LinkEstimate,
} from '../../capture/send-budget';

const BUDGET = { budgetMs: 100, maxBytes: 100_000 };

/** Observe the queue at each [ms, bufferedAmount], sending `sendBytes` after each read. */
function replay(reads: readonly (readonly [number, number])[], sendBytes: number): LinkEstimate {
  return reads.reduce<LinkEstimate>(
    (estimate, [ms, buffered]) => recordSend(observeQueue(estimate, ms, buffered), sendBytes),
    NO_ESTIMATE,
  );
}

describe('SEND_BUDGET', () => {
  it('keeps a queued message well inside the motion freshness limit (250 ms)', () => {
    expect(SEND_BUDGET.budgetMs).toBeLessThanOrEqual(100);
  });

  it('never queues more than about two 640 px JPEGs', () => {
    expect(SEND_BUDGET.maxBytes).toBeLessThanOrEqual(100_000);
  });
});

describe('queueLimitBytes', () => {
  it('uses the byte cap before the link has been measured', () => {
    expect(queueLimitBytes(NO_ESTIMATE, BUDGET)).toBe(100_000);
  });

  it('allows what the measured link drains within the time budget', () => {
    expect(queueLimitBytes({ ...NO_ESTIMATE, bytesPerMs: 500 }, BUDGET)).toBe(50_000);
  });

  it('never exceeds the byte cap on a fast link', () => {
    expect(queueLimitBytes({ ...NO_ESTIMATE, bytesPerMs: 10_000 }, BUDGET)).toBe(100_000);
  });
});

describe('fitsBudget', () => {
  it('always lets a message onto an empty queue, so a slow link still gets frames', () => {
    expect(fitsBudget(0, 60_000, 50_000)).toBe(true);
  });

  it('counts the message itself against the limit', () => {
    expect(fitsBudget(50_000, 50_000, 100_000)).toBe(true);
    expect(fitsBudget(50_001, 50_000, 100_000)).toBe(false);
  });
});

describe('observeQueue', () => {
  it('measures the link from what drained while the queue stayed busy', () => {
    // 40 KB sent every 20 ms; 1000 bytes/ms drains 20 KB of it per interval.
    const reads = Array.from({ length: 12 }, (_, k) => [k * 20, 20_000 * k + 1] as const);
    expect(replay(reads, 40_000).bytesPerMs).toBeCloseTo(1000, 0);
  });

  it('does not judge the link until it has watched it for a while', () => {
    expect(replay([[0, 1], [20, 20_001]], 40_000).bytesPerMs).toBeNull();
  });

  it('does not credit a whole message to the moment Safari reports it gone', () => {
    // 40 KB queued at 0; bufferedAmount stays put, then drops all at once at 200 ms.
    const queued = recordSend(observeQueue(NO_ESTIMATE, 0, 0), 40_000);
    const waiting = observeQueue(queued, 195, 40_000);
    expect(observeQueue(waiting, 200, 0).bytesPerMs).toBe(200);
  });

  it('ignores time when nothing was queued', () => {
    const idle = observeQueue(observeQueue(NO_ESTIMATE, 0, 0), 500, 0);
    expect(idle.windowMs).toBe(0);
    expect(idle.bytesPerMs).toBeNull();
  });

  it('treats several reads in the same millisecond as one', () => {
    const start = recordSend(observeQueue(NO_ESTIMATE, 0, 0), 40_000);
    const again = observeQueue(start, 0, 40_000);
    expect(again.atMs).toBe(0);
    expect(again.queuedBytes).toBe(40_000);
  });

  it('returns a new estimate rather than changing the old one', () => {
    const start = observeQueue(NO_ESTIMATE, 0, 0);
    recordSend(start, 1000);
    expect(start.queuedBytes).toBe(0);
    expect(Object.isFrozen(NO_ESTIMATE)).toBe(true);
  });
});
