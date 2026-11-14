import { describe, expect, it } from 'vitest';
import { circularMeanDeg, validNonNegative } from '../../shared/angles';

describe('circularMeanDeg', () => {
  it('averages across north instead of giving 180', () => {
    const mean = circularMeanDeg([358, 2]);
    expect(mean).not.toBeNull();
    expect(Math.min(mean!, 360 - mean!)).toBeLessThan(0.01);
  });

  it('returns the value for identical samples', () => {
    expect(circularMeanDeg([90, 90])).toBeCloseTo(90, 6);
  });

  it('normalises into [0, 360)', () => {
    const mean = circularMeanDeg([350, 340]);
    expect(mean).toBeCloseTo(345, 6);
    expect(circularMeanDeg([270])).toBeCloseTo(270, 6);
  });

  it('returns null with no samples', () => {
    expect(circularMeanDeg([])).toBeNull();
  });

  it('returns null when the directions cancel out', () => {
    expect(circularMeanDeg([90, 270])).toBeNull();
  });
});

describe('validNonNegative', () => {
  it('keeps only finite non-negative values', () => {
    expect([-1, 10, Number.NaN, Number.POSITIVE_INFINITY, 0].filter(validNonNegative)).toEqual([10, 0]);
  });

  it('rejects null', () => {
    expect(validNonNegative(null)).toBe(false);
  });
});
