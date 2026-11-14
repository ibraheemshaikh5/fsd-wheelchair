import { describe, expect, it } from 'vitest';
import type { GpsStatus, HeadingStatus, StatusRow } from '../../shared/status';
import { createChangeGate, createRowsGate, mapUpdateKey } from '../../viewer/render-gate';
import { frameBadge } from '../../viewer/format';

const GPS: GpsStatus = {
  lat: 38.98691,
  lon: -76.94262,
  accuracy_m: 4.2,
  altitude_m: 40,
  speed_mps: 1.25,
  course_deg: 90,
  age_ms: 300,
  fresh: true,
};

const HEADING: HeadingStatus = {
  deg: 90,
  source: 'compass',
  reference: 'true',
  declination_deg: -11,
  compass_deg: 101,
  compass_accuracy_deg: 10,
  compass_age_ms: 50,
  compass_fresh: true,
};

describe('createChangeGate', () => {
  it('lets a key through only when it differs from the last one', () => {
    const changed = createChangeGate();
    expect(changed('a')).toBe(true);
    expect(changed('a')).toBe(false);
    expect(changed('b')).toBe(true);
    expect(changed('a')).toBe(true);
  });
});

describe('createRowsGate', () => {
  const INTERVAL_MS = 250;
  const rows = (age: string, level: StatusRow['level'] = 'ok'): readonly StatusRow[] => [
    { label: 'Phone', value: 'Connected', level: 'ok' },
    { label: 'Camera', value: `30.1 fps · age ${age}`, level },
  ];

  function setup() {
    const clock = { ms: 0 };
    return { clock, changed: createRowsGate(INTERVAL_MS, () => clock.ms) };
  }

  it('draws the first rows straight away', () => {
    const { changed } = setup();
    expect(changed(rows('40 ms'))).toBe(true);
  });

  it('redraws text that changes every tick (ages, fps) at most once per interval', () => {
    const { clock, changed } = setup();
    changed(rows('40 ms'));
    const draws = [1, 2, 3, 4, 5, 6, 7, 8].map((tick) => {
      clock.ms = tick * 33;
      return changed(rows(`${40 + tick} ms`));
    });
    // About 30 statuses a second, but only one redraw per 250 ms.
    expect(draws.filter(Boolean)).toHaveLength(1);
    expect(draws.at(-1)).toBe(true);
  });

  it('redraws at once when a row’s level changes, so a failure shows without waiting', () => {
    const { clock, changed } = setup();
    changed(rows('40 ms'));
    clock.ms = 10;
    expect(changed(rows('41 ms', 'bad'))).toBe(true);
  });

  it('skips identical rows even once the interval has passed', () => {
    const { clock, changed } = setup();
    changed(rows('40 ms'));
    clock.ms = INTERVAL_MS * 4;
    expect(changed(rows('40 ms'))).toBe(false);
  });
});

describe('mapUpdateKey', () => {
  it('ignores the ages that change on every 30 Hz status', () => {
    const older = mapUpdateKey({ ...GPS, age_ms: 900 }, { ...HEADING, compass_age_ms: 400 });
    expect(mapUpdateKey(GPS, HEADING)).toBe(older);
  });

  it('changes with anything the map draws', () => {
    const base = mapUpdateKey(GPS, HEADING);
    expect(mapUpdateKey({ ...GPS, lat: 38.9 }, HEADING)).not.toBe(base);
    expect(mapUpdateKey({ ...GPS, fresh: false }, HEADING)).not.toBe(base);
    expect(mapUpdateKey(GPS, { ...HEADING, deg: 91 })).not.toBe(base);
    expect(mapUpdateKey(GPS, null)).not.toBe(base);
    expect(mapUpdateKey(null, null)).not.toBe(mapUpdateKey(GPS, null));
  });
});

describe('frameBadge with undecodable frames', () => {
  const frame = { seq: 5, w: 640, h: 480, age_ms: 40, fresh: true };

  it('notes frames the viewer could not decode', () => {
    expect(frameBadge(frame, true, 2)).toBe('#5 · 40 ms old · 2 undecodable');
    expect(frameBadge(frame, true, 0)).toBe('#5 · 40 ms old');
  });
});
