// Statuses arrive about 30 times a second. These keep the viewer from redrawing
// the status list and the map when nothing they show has changed.

import type { GpsStatus, HeadingStatus, StatusRow } from '../shared/status';

/** Returns true when `key` differs from the key it was last called with. */
export function createChangeGate(): (key: string) => boolean {
  let last: string | null = null;
  return (key) => {
    if (key === last) return false;
    last = key;
    return true;
  };
}

/** Everything the map draws from a status, without the ages that change on every tick. */
export function mapUpdateKey(gps: GpsStatus | null, heading: HeadingStatus | null): string {
  const position = gps === null ? null : { ...gps, age_ms: null };
  const facing = heading === null ? null : { ...heading, compass_age_ms: null };
  return JSON.stringify([position, facing]);
}

/** How often the status list may redraw for text alone (ages, fps change on every status). */
export const ROWS_REDRAW_MS = 250;

/** What each row says about health; a change here redraws straight away. */
const levelsKey = (rows: readonly StatusRow[]): string => rows.map((row) => `${row.label}=${row.level}`).join('\n');

/**
 * Returns true when the status list should redraw: at once when a row appears,
 * goes or changes level, otherwise at most every `intervalMs` and only if its
 * text changed. Statuses keep coming on the service's tick, so the newest text
 * is drawn within one interval.
 */
export function createRowsGate(intervalMs: number, now: () => number): (rows: readonly StatusRow[]) => boolean {
  let levels: string | null = null;
  let text: string | null = null;
  let drawnAtMs = Number.NEGATIVE_INFINITY;
  return (rows) => {
    const nextLevels = levelsKey(rows);
    const levelsChanged = nextLevels !== levels;
    if (!levelsChanged && now() - drawnAtMs < intervalMs) return false;
    const nextText = JSON.stringify(rows);
    if (!levelsChanged && nextText === text) return false;
    levels = nextLevels;
    text = nextText;
    drawnAtMs = now();
    return true;
  };
}
