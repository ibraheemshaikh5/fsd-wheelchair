// Pure summaries for the device check report.

import { circularMeanDeg, normaliseDeg, validNonNegative } from '../shared/angles';

/** How many raw samples to show at each end, so a known bearing can be compared by eye. */
export const EDGE_SAMPLES = 5;
const ONE_DECIMAL = 10;

export interface Stats {
  readonly count: number;
  readonly min: number | null;
  readonly max: number | null;
  readonly mean: number | null;
}

export interface CourseStats {
  readonly count: number;
  readonly circularMeanDeg: number | null;
  readonly first: readonly number[];
  readonly last: readonly number[];
}

const roundOneDecimal = (value: number): number => Math.round(value * ONE_DECIMAL) / ONE_DECIMAL;

/** Count, min, max and mean of linear values (not angles: see courseStats). */
export function summarise(values: readonly number[]): Stats {
  if (values.length === 0) return { count: 0, min: null, max: null, mean: null };
  const total = values.reduce((sum, value) => sum + value, 0);
  return {
    count: values.length,
    min: Math.min(...values),
    max: Math.max(...values),
    mean: roundOneDecimal(total / values.length),
  };
}

/** Gaps between consecutive timestamps. */
export function intervals(times: readonly number[]): number[] {
  return times.slice(1).map((t, i) => t - (times[i] ?? t));
}

/** A new list with `value` appended if it is a real reading (finite, >= 0). */
export function keepValid(values: readonly number[], value: number | null): readonly number[] {
  return validNonNegative(value) ? [...values, value] : values;
}

/** Course is an angle: a circular mean, plus raw first and last samples. */
export function courseStats(values: readonly number[]): CourseStats {
  const mean = circularMeanDeg(values);
  return {
    count: values.length,
    circularMeanDeg: mean === null ? null : normaliseDeg(roundOneDecimal(mean)),
    first: values.slice(0, EDGE_SAMPLES),
    last: values.slice(-EDGE_SAMPLES),
  };
}
