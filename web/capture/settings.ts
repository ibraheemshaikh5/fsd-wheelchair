import { DEFAULT_CAPTURE_FPS, MAX_CAMERA_FPS } from './frame-rate';
import { AUTO_ROTATION, ROTATION_SETTINGS, type RotationSetting } from './orientation';

/** Capture settings the rider's helper can change on the phone, saved between visits. */
export interface CaptureSettings {
  /** Camera lens height above the ground. */
  readonly mountHeightM: number;
  /** Camera tilt; negative means pointing down. */
  readonly mountPitchDeg: number;
  /** Most frames sent per second; the camera runs as fast as it can up to this. */
  readonly fps: number;
  /** Longest side of the JPEG sent to the service. */
  readonly maxWidth: number;
  readonly jpegQuality: number;
  /** Clockwise quarter turn for every frame, or AUTO_ROTATION to follow gravity (landscape when sideways). */
  readonly rotateDeg: RotationSetting;
}

/**
 * Bumped when a default changes in a way older saved settings should not keep.
 * 2: fps went from 10 to 60, so settings saved before then drop their fps.
 * 3: the fps setting may go up to 240 (opt-in slow-motion rates).
 * 4: driving defaults (20 fps, 480 px), so the 60 fps and 640 px saved before drop.
 */
export const SETTINGS_VERSION = 4;

// Sized for driving: 20 fps (see DEFAULT_CAPTURE_FPS) and 480 px, above every
// model's input (FlowPilot's 384 px is the largest), so a weak link carries
// about a third of what 60 fps at 640 px did. For training data, set 60 fps and
// 640 px. Higher settings ask the camera for up to its top rate.
export const DEFAULT_SETTINGS: CaptureSettings = Object.freeze({
  mountHeightM: 1.1,
  mountPitchDeg: -10,
  fps: DEFAULT_CAPTURE_FPS,
  maxWidth: 480,
  jpegQuality: 0.7,
  rotateDeg: AUTO_ROTATION,
});

type Range = readonly [min: number, max: number];

type NumericSetting = Exclude<keyof CaptureSettings, 'rotateDeg'>;

// Kept inside what the service accepts (see MountInfo / CameraInfo there).
const LIMITS: Record<NumericSetting, Range> = {
  mountHeightM: [0.1, 3],
  mountPitchDeg: [-90, 90],
  fps: [1, MAX_CAMERA_FPS],
  maxWidth: [160, 1920],
  jpegQuality: [0.3, 0.95],
};

/** Fields whose saved value is only trusted from the current settings version. */
const VERSIONED_FIELDS: ReadonlySet<keyof CaptureSettings> = new Set(['fps', 'maxWidth']);

const inRange = (value: unknown, [min, max]: Range): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;

/** Keep each valid field and default the rest. */
export function validSettings(values: Readonly<Record<string, unknown>>): CaptureSettings {
  const pick = (key: NumericSetting): number => {
    const value = values[key];
    return inRange(value, LIMITS[key]) ? value : DEFAULT_SETTINGS[key];
  };
  return {
    mountHeightM: pick('mountHeightM'),
    mountPitchDeg: pick('mountPitchDeg'),
    fps: pick('fps'),
    maxWidth: pick('maxWidth'),
    jpegQuality: pick('jpegQuality'),
    rotateDeg: rotationSetting(values['rotateDeg']),
  };
}

const rotationSetting = (value: unknown): RotationSetting =>
  ROTATION_SETTINGS.find((option) => option === value) ?? AUTO_ROTATION;

/** Saved values minus the ones an older settings version should not carry forward. */
function migrate(saved: Readonly<Record<string, unknown>>): Readonly<Record<string, unknown>> {
  if (saved['version'] === SETTINGS_VERSION) return saved;
  return Object.fromEntries(
    Object.entries(saved).filter(([key]) => !VERSIONED_FIELDS.has(key as keyof CaptureSettings)),
  );
}

/** Read saved settings, keeping each valid field and defaulting the rest. */
export function parseSettings(raw: string | null): CaptureSettings {
  if (raw === null) return DEFAULT_SETTINGS;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return DEFAULT_SETTINGS;
  }
  if (typeof data !== 'object' || data === null) return DEFAULT_SETTINGS;
  return validSettings(migrate(data as Record<string, unknown>));
}

/** The form parseSettings reads back, stamped with the current version. */
export function serializeSettings(settings: CaptureSettings): string {
  return JSON.stringify({ version: SETTINGS_VERSION, ...settings });
}

export function phoneSocketUrl(location: { protocol: string; host: string }): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/ws/phone`;
}
