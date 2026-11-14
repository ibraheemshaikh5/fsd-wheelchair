import { describe, expect, it } from 'vitest';
import { DEFAULT_SETTINGS, parseSettings, phoneSocketUrl } from '../../capture/settings';

describe('parseSettings', () => {
  it('returns defaults for missing or broken input', () => {
    expect(parseSettings(null)).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings('{oops')).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings('42')).toEqual(DEFAULT_SETTINGS);
  });

  it('keeps valid saved values', () => {
    const saved = JSON.stringify({ mountHeightM: 0.9, mountPitchDeg: -15, fps: 8, maxWidth: 480, jpegQuality: 0.6 });
    expect(parseSettings(saved)).toEqual({
      mountHeightM: 0.9,
      mountPitchDeg: -15,
      fps: 8,
      maxWidth: 480,
      jpegQuality: 0.6,
    });
  });

  it('replaces out-of-range or wrongly typed values with defaults, field by field', () => {
    const saved = JSON.stringify({ mountHeightM: 9, mountPitchDeg: 'down', fps: 0, maxWidth: 480, jpegQuality: 2 });
    expect(parseSettings(saved)).toEqual({ ...DEFAULT_SETTINGS, maxWidth: 480 });
  });
});

describe('phoneSocketUrl', () => {
  it('uses wss on https pages', () => {
    expect(phoneSocketUrl({ protocol: 'https:', host: 'chair.tailnet.ts.net' })).toBe(
      'wss://chair.tailnet.ts.net/ws/phone',
    );
  });

  it('uses ws on plain http pages', () => {
    expect(phoneSocketUrl({ protocol: 'http:', host: '127.0.0.1:8000' })).toBe('ws://127.0.0.1:8000/ws/phone');
  });
});
