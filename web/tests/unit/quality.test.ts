import { describe, expect, it } from 'vitest';
import {
  adjustQuality,
  initialQuality,
  MIN_SENT_FPS,
  QUALITY_FLOOR,
  type QualityState,
  type QualityWindow,
  RTT_HIGH_MS,
  RTT_LOW_MS,
} from '../../capture/quality';

/** One second at 60 fps capture. */
const window = (overrides: Partial<QualityWindow> = {}): QualityWindow => ({
  seconds: 1,
  captured: 60,
  sent: 40,
  skipped: 20,
  ackRttMs: 50,
  ...overrides,
});

const steps = (start: QualityState, w: QualityWindow, n: number): number[] => {
  let state = start;
  const seen: number[] = [];
  for (let i = 0; i < n; i += 1) {
    state = adjustQuality(state, w);
    seen.push(state.quality);
  }
  return seen;
};

describe('adaptive JPEG quality', () => {
  it('starts at the configured quality', () => {
    expect(initialQuality(0.7)).toEqual({ quality: 0.7, ceiling: 0.7 });
  });

  describe('with acks (judged by ack round trip and frames delivered)', () => {
    it('keeps full quality on a quick link even though most of 60 fps is held back', () => {
      // 3 frames in flight at a 60 ms round trip: ~40 of 60 frames go out, all fresh.
      const start = { quality: 0.7, ceiling: 0.7 };
      expect(adjustQuality(start, window({ sent: 38, skipped: 22, ackRttMs: 60 }))).toEqual(start);
    });

    it('steps down by 0.1 to the floor while acks take longer than 150 ms', () => {
      const slow = window({ sent: 30, skipped: 30, ackRttMs: RTT_HIGH_MS + 50 });
      expect(steps(initialQuality(0.7), slow, 5)).toEqual([0.6, 0.5, QUALITY_FLOOR, QUALITY_FLOOR, QUALITY_FLOOR]);
    });

    it('steps down when fewer than 20 frames a second get through, even with quick acks', () => {
      const starved = window({ sent: MIN_SENT_FPS - 5, skipped: 45, ackRttMs: 80 });
      expect(adjustQuality(initialQuality(0.7), starved).quality).toBe(0.6);
    });

    it('at a 20 fps camera, does not step down for a frame or two lost to timing jitter', () => {
      const start = { quality: 0.6, ceiling: 0.7 };
      const jitter = window({ captured: 20, sent: 19, skipped: 1, ackRttMs: 40 });
      expect(adjustQuality(start, jitter).quality).toBe(0.65);
      const thinned = window({ captured: 19, sent: 18, skipped: 1, ackRttMs: 40 });
      expect(adjustQuality(start, thinned).quality).toBe(0.65);
    });

    it('at a 20 fps camera, steps down once the link loses more than a couple of frames a second', () => {
      const lossy = window({ captured: 20, sent: 16, skipped: 4, ackRttMs: 40 });
      expect(adjustQuality(initialQuality(0.7), lossy).quality).toBe(0.6);
    });

    it('does not blame the link when the camera itself delivers under 20 fps', () => {
      const start = { quality: 0.6, ceiling: 0.7 };
      const slowCamera = window({ captured: 15, sent: 15, skipped: 0, ackRttMs: 40 });
      expect(adjustQuality(start, slowCamera).quality).toBe(0.65);
    });

    it('steps up by 0.05 when acks are under 100 ms and 20+ fps get through, to the setting', () => {
      const healthy = window({ sent: 45, skipped: 15, ackRttMs: RTT_LOW_MS - 30 });
      expect(steps({ quality: 0.5, ceiling: 0.7 }, healthy, 6)).toEqual([0.55, 0.6, 0.65, 0.7, 0.7, 0.7]);
    });

    it('leaves a wide hold band at the driving rate: 2 frames in flight carry 20 fps up to a 100 ms round trip', () => {
      // Stepping up only well under 100 ms keeps a link near the 2-frame limit from bouncing.
      expect(RTT_LOW_MS).toBe(75);
      const start = { quality: 0.55, ceiling: 0.7 };
      expect(adjustQuality(start, window({ captured: 20, sent: 20, skipped: 0, ackRttMs: 90 }))).toBe(start);
    });

    it('holds between 75 and 150 ms', () => {
      const start = { quality: 0.55, ceiling: 0.7 };
      expect(adjustQuality(start, window({ ackRttMs: 120 }))).toBe(start);
    });

    it('scales by window length', () => {
      // 2 s with 30 frames sent is 15 fps: starved.
      const start = initialQuality(0.7);
      expect(adjustQuality(start, window({ seconds: 2, captured: 120, sent: 30, ackRttMs: 50 })).quality).toBe(0.6);
    });
  });

  describe('without acks (an older service): share of frames held back', () => {
    it('steps down when more than 30% were held back and up when under 5%', () => {
      expect(adjustQuality(initialQuality(0.7), window({ ackRttMs: null, skipped: 19 })).quality).toBe(0.6);
      expect(adjustQuality({ quality: 0.5, ceiling: 0.7 }, window({ ackRttMs: null, skipped: 2 })).quality).toBe(0.55);
      const start = { quality: 0.55, ceiling: 0.7 };
      expect(adjustQuality(start, window({ ackRttMs: null, skipped: 10 }))).toBe(start);
    });
  });

  it('changes nothing on an empty window', () => {
    const start = { quality: 0.55, ceiling: 0.7 };
    expect(adjustQuality(start, window({ captured: 0, sent: 0, skipped: 0 }))).toBe(start);
    expect(adjustQuality(start, window({ seconds: 0 }))).toBe(start);
  });

  it('never goes below a setting that is itself under the floor', () => {
    const slow = window({ ackRttMs: 500 });
    expect(adjustQuality(initialQuality(0.3), slow).quality).toBe(0.3);
    expect(adjustQuality(initialQuality(0.45), slow).quality).toBe(QUALITY_FLOOR);
  });
});
