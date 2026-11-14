import { describe, expect, it } from 'vitest';
import { DEFAULT_CAPTURE_FPS, MAX_CAMERA_FPS } from '../../capture/frame-rate';
import { AUTO_ROTATION } from '../../capture/orientation';
import {
  DEFAULT_SETTINGS,
  parseSettings,
  phoneSocketUrl,
  serializeSettings,
  SETTINGS_VERSION,
  validSettings,
} from '../../capture/settings';

const saved = (values: Record<string, unknown>): string => JSON.stringify({ version: SETTINGS_VERSION, ...values });

describe('DEFAULT_SETTINGS', () => {
  it('runs at 60 fps by default: the top normal video rate, three frames per 20 Hz model step', () => {
    expect(DEFAULT_SETTINGS.fps).toBe(DEFAULT_CAPTURE_FPS);
    expect(DEFAULT_CAPTURE_FPS).toBe(60);
  });

  it('allows opting into faster (slow-motion) camera rates up to what the service accepts', () => {
    expect(MAX_CAMERA_FPS).toBe(240);
  });
});

describe('parseSettings', () => {
  it('returns defaults for missing or broken input', () => {
    expect(parseSettings(null)).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings('{oops')).toEqual(DEFAULT_SETTINGS);
    expect(parseSettings('42')).toEqual(DEFAULT_SETTINGS);
  });

  it('keeps valid saved values', () => {
    const raw = saved({ mountHeightM: 0.9, mountPitchDeg: -15, fps: 8, maxWidth: 480, jpegQuality: 0.6 });
    expect(parseSettings(raw)).toEqual({
      mountHeightM: 0.9,
      mountPitchDeg: -15,
      fps: 8,
      maxWidth: 480,
      jpegQuality: 0.6,
      rotateDeg: AUTO_ROTATION,
    });
  });

  it('accepts up to the service’s 240 fps ceiling and no more', () => {
    expect(parseSettings(saved({ fps: 120 })).fps).toBe(120);
    expect(parseSettings(saved({ fps: 240 })).fps).toBe(240);
    expect(parseSettings(saved({ fps: 241 })).fps).toBe(DEFAULT_SETTINGS.fps);
  });

  it('drops fps saved by version 2, so the current default applies', () => {
    const v2 = JSON.stringify({ version: 2, mountHeightM: 0.9, fps: 60 });
    expect(SETTINGS_VERSION).toBe(3);
    expect(parseSettings(v2)).toEqual({ ...DEFAULT_SETTINGS, mountHeightM: 0.9 });
  });

  it('replaces out-of-range or wrongly typed values with defaults, field by field', () => {
    const raw = saved({ mountHeightM: 9, mountPitchDeg: 'down', fps: 0, maxWidth: 480, jpegQuality: 2 });
    expect(parseSettings(raw)).toEqual({ ...DEFAULT_SETTINGS, maxWidth: 480 });
  });

  it('drops the old 10 fps from settings saved before versioning, keeping the mount', () => {
    const old = JSON.stringify({ mountHeightM: 0.9, mountPitchDeg: -15, fps: 10, maxWidth: 480, jpegQuality: 0.6 });
    expect(parseSettings(old)).toEqual({
      mountHeightM: 0.9,
      mountPitchDeg: -15,
      fps: DEFAULT_SETTINGS.fps,
      maxWidth: 480,
      jpegQuality: 0.6,
      rotateDeg: AUTO_ROTATION,
    });
  });

  it('treats an older version number like unversioned settings', () => {
    const old = JSON.stringify({ version: SETTINGS_VERSION - 1, mountHeightM: 0.9, fps: 10 });
    expect(parseSettings(old)).toEqual({ ...DEFAULT_SETTINGS, mountHeightM: 0.9 });
  });
});

describe('serializeSettings', () => {
  it('round-trips through parseSettings with the current version', () => {
    const settings = { ...DEFAULT_SETTINGS, fps: 24, mountHeightM: 0.8 };
    const raw = serializeSettings(settings);
    expect(JSON.parse(raw)).toMatchObject({ version: SETTINGS_VERSION });
    expect(parseSettings(raw)).toEqual(settings);
  });
});

describe('validSettings', () => {
  it('checks form values without any version gate', () => {
    expect(validSettings({ ...DEFAULT_SETTINGS, fps: 15 })).toEqual({ ...DEFAULT_SETTINGS, fps: 15 });
    expect(validSettings({ fps: Number.NaN })).toEqual(DEFAULT_SETTINGS);
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

describe('rotateDeg setting', () => {
  it('follows gravity by default', () => {
    expect(DEFAULT_SETTINGS.rotateDeg).toBe(AUTO_ROTATION);
  });

  it('keeps a saved fixed quarter turn and refuses anything else', () => {
    expect(parseSettings(saved({ rotateDeg: 90 })).rotateDeg).toBe(90);
    expect(parseSettings(saved({ rotateDeg: 0 })).rotateDeg).toBe(0);
    expect(parseSettings(saved({ rotateDeg: 45 })).rotateDeg).toBe(AUTO_ROTATION);
    expect(parseSettings(saved({ rotateDeg: '90' })).rotateDeg).toBe(AUTO_ROTATION);
  });
});
