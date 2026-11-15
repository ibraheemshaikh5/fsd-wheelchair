import { describe, expect, it } from 'vitest';
import { monotonicToWallMs, sampleFromPong } from '../../capture/clock';

describe('sampleFromPong', () => {
  it('computes round trip and offset assuming a symmetric network', () => {
    // Sent at 1000 (phone), server stamped 5050, received at 1100 (phone).
    const sample = sampleFromPong({ v: 1, type: 'pong', t: 1000, server_t: 5050 }, 1100);
    expect(sample).toEqual({ rtt_ms: 100, offset_ms: 4000 });
  });

  it('ignores a pong that arrives before it was sent', () => {
    expect(sampleFromPong({ v: 1, type: 'pong', t: 1000, server_t: 1 }, 999)).toBeNull();
  });
});

describe('monotonicToWallMs', () => {
  const WALL_NOW = 2_000_000;
  const MONO_NOW = 1_000;

  it('subtracts the delay measured on the monotonic clock from the wall clock', () => {
    expect(monotonicToWallMs(960, WALL_NOW, MONO_NOW)).toBe(WALL_NOW - 40);
  });

  it('never stamps a time later than now', () => {
    expect(monotonicToWallMs(1_016, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
  });

  it('falls back to now with no time, or one older than the allowed delay', () => {
    expect(monotonicToWallMs(undefined, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
    expect(monotonicToWallMs(Number.NaN, WALL_NOW, MONO_NOW)).toBe(WALL_NOW);
    expect(monotonicToWallMs(0, WALL_NOW, MONO_NOW, 500)).toBe(WALL_NOW);
    expect(monotonicToWallMs(600, WALL_NOW, MONO_NOW, 500)).toBe(WALL_NOW - 400);
  });
});
