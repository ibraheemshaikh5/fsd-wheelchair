// What the map marker should look like. Pure, so it can be unit tested
// without Leaflet. A bright arrow is drawn only for a true-north heading with a
// fresh fix: a magnetic heading is ~11° off the map's north and is dimmed.

import { normaliseDeg } from '../shared/angles';
import type { HeadingStatus } from '../shared/status';

export const MARKER_SIZE_PX = 28;
const DIMMED_OPACITY = 0.4;
export const MARKER_COLOR = '#4c8dff';
const DESTINATION_COLOR = '#3ecf8e';
export const DESTINATION_SIZE_PX = 32;

export interface MarkerShape {
  readonly kind: 'arrow' | 'dot';
  /** Whole degrees in [0, 360) for an arrow, null for a dot. */
  readonly deg: number | null;
  readonly dimmed: boolean;
}

export function markerShape(heading: HeadingStatus | null, gpsFresh: boolean): MarkerShape {
  if (heading === null || heading.deg === null) return { kind: 'dot', deg: null, dimmed: !gpsFresh };
  const trueNorth = heading.reference === 'true';
  return { kind: 'arrow', deg: normaliseDeg(Math.round(heading.deg)), dimmed: !gpsFresh || !trueNorth };
}

/** The same shape, dimmed: used when the viewer loses the service. */
export function dimmed(shape: MarkerShape): MarkerShape {
  return { ...shape, dimmed: true };
}

/** Changes whenever the drawn icon would change, so the icon is rebuilt only then. */
export function markerKey(shape: MarkerShape): string {
  return `${shape.kind}:${shape.deg ?? '-'}:${shape.dimmed ? 'dim' : 'lit'}`;
}

export function markerSvg(shape: MarkerShape): string {
  const opacity = shape.dimmed ? DIMMED_OPACITY : 1;
  const body =
    shape.kind === 'arrow' && shape.deg !== null
      ? `<g transform="rotate(${shape.deg} 14 14)"><path d="M14 2 L24 24 L14 19 L4 24 Z" fill="${MARKER_COLOR}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></g>`
      : `<circle cx="14" cy="14" r="7" fill="${MARKER_COLOR}" stroke="#fff" stroke-width="2"/>`;
  return `<svg width="${MARKER_SIZE_PX}" height="${MARKER_SIZE_PX}" viewBox="0 0 28 28" aria-hidden="true"><g opacity="${opacity}">${body}</g></svg>`;
}

/**
 * The destination pin. A pending pin (tapped, not yet sent to the service) is
 * hollow, so it never looks like a destination the service knows about.
 */
export function destinationSvg(pending: boolean): string {
  const fill = pending ? 'none' : DESTINATION_COLOR;
  const dash = pending ? ' stroke-dasharray="3 2"' : '';
  return `<svg width="${DESTINATION_SIZE_PX}" height="${DESTINATION_SIZE_PX}" viewBox="0 0 32 32" aria-hidden="true"><path d="M16 30 C16 30 5 18.5 5 12 a11 11 0 0 1 22 0 C27 18.5 16 30 16 30 Z" fill="${fill}" stroke="${DESTINATION_COLOR}" stroke-width="2.5"${dash}/><circle cx="16" cy="12" r="4" fill="#0d0f12"/></svg>`;
}
