import { describe, expect, it } from 'vitest';
import type { HeadingStatus } from '../../shared/status';
import { destinationSvg, dimmed, markerKey, markerShape, markerSvg } from '../../shared/marker';

const TRUE_HEADING: HeadingStatus = {
  deg: 79.4,
  source: 'compass',
  reference: 'true',
  declination_deg: -11,
  compass_deg: 90.4,
  compass_accuracy_deg: 10,
  compass_age_ms: 50,
  compass_fresh: true,
};

describe('markerShape', () => {
  it('draws a bright arrow only for a true-north heading with a fresh fix', () => {
    expect(markerShape(TRUE_HEADING, true)).toEqual({ kind: 'arrow', deg: 79, dimmed: false });
  });

  it('dims the arrow when the fix is stale', () => {
    expect(markerShape(TRUE_HEADING, false)).toEqual({ kind: 'arrow', deg: 79, dimmed: true });
  });

  it('never draws a bright arrow for a magnetic heading', () => {
    const magnetic: HeadingStatus = { ...TRUE_HEADING, deg: 90.4, reference: 'magnetic', declination_deg: null };
    expect(markerShape(magnetic, true)).toEqual({ kind: 'arrow', deg: 90, dimmed: true });
  });

  it('never draws a bright arrow when the reference is missing', () => {
    const unknown: HeadingStatus = { ...TRUE_HEADING, reference: null };
    expect(markerShape(unknown, true).dimmed).toBe(true);
  });

  it('draws a dot when there is no heading', () => {
    expect(markerShape(null, true)).toEqual({ kind: 'dot', deg: null, dimmed: false });
    expect(markerShape({ ...TRUE_HEADING, deg: null }, true)).toEqual({ kind: 'dot', deg: null, dimmed: false });
    expect(markerShape(null, false)).toEqual({ kind: 'dot', deg: null, dimmed: true });
  });

  it('wraps a rounded 360 to 0', () => {
    expect(markerShape({ ...TRUE_HEADING, deg: 359.7 }, true).deg).toBe(0);
  });
});

describe('markerSvg', () => {
  it('rotates the arrow and dims it when asked', () => {
    const bright = markerSvg({ kind: 'arrow', deg: 79, dimmed: false });
    expect(bright).toContain('rotate(79 ');
    expect(bright).toContain('opacity="1"');
    expect(markerSvg({ kind: 'arrow', deg: 79, dimmed: true })).toContain('opacity="0.4"');
  });

  it('draws a circle for a dot', () => {
    const dot = markerSvg({ kind: 'dot', deg: null, dimmed: true });
    expect(dot).toContain('<circle');
    expect(dot).not.toContain('rotate(');
  });
});

describe('markerKey', () => {
  it('changes when any visible property changes', () => {
    const a = markerKey({ kind: 'arrow', deg: 79, dimmed: false });
    expect(markerKey({ kind: 'arrow', deg: 79, dimmed: false })).toBe(a);
    expect(markerKey({ kind: 'arrow', deg: 80, dimmed: false })).not.toBe(a);
    expect(markerKey({ kind: 'arrow', deg: 79, dimmed: true })).not.toBe(a);
    expect(markerKey({ kind: 'dot', deg: null, dimmed: false })).not.toBe(a);
  });
});

describe('dimmed', () => {
  it('returns a dimmed copy without changing the original', () => {
    const lit = { kind: 'arrow', deg: 79, dimmed: false } as const;
    expect(dimmed(lit)).toEqual({ kind: 'arrow', deg: 79, dimmed: true });
    expect(lit.dimmed).toBe(false);
  });
});

describe('destinationSvg', () => {
  it('draws a filled pin for a set destination and a hollow dashed one while pending', () => {
    expect(destinationSvg(false)).toContain('fill="#3ecf8e"');
    expect(destinationSvg(false)).not.toContain('stroke-dasharray');
    expect(destinationSvg(true)).toContain('fill="none"');
    expect(destinationSvg(true)).toContain('stroke-dasharray');
  });
});
