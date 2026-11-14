/** Capture settings the rider's helper can change on the phone, saved between visits. */
export interface CaptureSettings {
  /** Camera lens height above the ground. */
  readonly mountHeightM: number;
  /** Camera tilt; negative means pointing down. */
  readonly mountPitchDeg: number;
  readonly fps: number;
  /** Longest side of the JPEG sent to the service. */
  readonly maxWidth: number;
  readonly jpegQuality: number;
}

export const DEFAULT_SETTINGS: CaptureSettings = Object.freeze({
  mountHeightM: 1.1,
  mountPitchDeg: -10,
  fps: 10,
  maxWidth: 640,
  jpegQuality: 0.7,
});

type Range = readonly [min: number, max: number];

// Kept inside what the service accepts (see MountInfo / CameraInfo there).
const LIMITS: Record<keyof CaptureSettings, Range> = {
  mountHeightM: [0.1, 3],
  mountPitchDeg: [-90, 90],
  fps: [1, 30],
  maxWidth: [160, 1920],
  jpegQuality: [0.3, 0.95],
};

const inRange = (value: unknown, [min, max]: Range): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;

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
  const saved = data as Record<string, unknown>;
  const pick = (key: keyof CaptureSettings): number => {
    const value = saved[key];
    return inRange(value, LIMITS[key]) ? value : DEFAULT_SETTINGS[key];
  };
  return {
    mountHeightM: pick('mountHeightM'),
    mountPitchDeg: pick('mountPitchDeg'),
    fps: pick('fps'),
    maxWidth: pick('maxWidth'),
    jpegQuality: pick('jpegQuality'),
  };
}

export function phoneSocketUrl(location: { protocol: string; host: string }): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/ws/phone`;
}
