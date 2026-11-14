import { describe, expect, it } from 'vitest';
import { courseStats, intervals, keepValid, summarise } from '../../spike/stats';

describe('summarise', () => {
  it('reports count, min, max and a rounded mean', () => {
    expect(summarise([1, 2, 4])).toEqual({ count: 3, min: 1, max: 4, mean: 2.3 });
    expect(summarise([])).toEqual({ count: 0, min: null, max: null, mean: null });
  });
});

describe('intervals', () => {
  it('gives the gaps between consecutive times', () => {
    expect(intervals([0, 100, 250])).toEqual([100, 150]);
    expect(intervals([5])).toEqual([]);
  });
});

describe('keepValid', () => {
  it('drops Safari placeholders (-1, NaN, null) for course and speed', () => {
    expect(keepValid([], -1)).toEqual([]);
    expect(keepValid(keepValid([], -1), 10)).toEqual([10]);
    expect(keepValid([3], Number.NaN)).toEqual([3]);
    expect(keepValid([3], null)).toEqual([3]);
  });

  it('builds a filtered list without mutating the input', () => {
    const input: readonly number[] = [1];
    const output = keepValid(input, 2);
    expect(input).toEqual([1]);
    expect(output).toEqual([1, 2]);
  });
});

describe('courseStats', () => {
  it('uses a circular mean so courses either side of north average near north', () => {
    const stats = courseStats([358, 359, 1, 2]);
    expect(stats.count).toBe(4);
    expect(stats.circularMeanDeg).toBe(0);
    expect(stats.first).toEqual([358, 359, 1, 2]);
    expect(stats.last).toEqual([358, 359, 1, 2]);
  });

  it('reports the first and last samples', () => {
    const values = [10, 11, 12, 13, 14, 15, 16, 17];
    const stats = courseStats(values);
    expect(stats.first).toEqual([10, 11, 12, 13, 14]);
    expect(stats.last).toEqual([13, 14, 15, 16, 17]);
    expect(stats.circularMeanDeg).toBeCloseTo(13.5, 1);
  });

  it('reports no mean without samples', () => {
    expect(courseStats([])).toEqual({ count: 0, circularMeanDeg: null, first: [], last: [] });
  });
});
