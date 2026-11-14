import { monotonicToWallMs } from './clock';
import { isPlausibleFps } from './frame-rate';

export interface DeviceInfoLike {
  readonly kind: string;
  readonly deviceId: string;
  readonly label: string;
}

export interface CaptureSize {
  readonly width: number;
  readonly height: number;
  readonly fps: number;
}

const BACK = /back|rear|environment/i;
const ULTRA_WIDE = /ultra\s*wide/i;
// Multi-camera virtual devices switch lenses on their own; telephoto narrows the view.
const AVOID = /telephoto|dual|triple|front/i;

/**
 * Choose the rear camera with the widest view. Labels are only visible after
 * camera permission is granted, so call this after the first getUserMedia.
 */
export function pickRearCamera(devices: readonly DeviceInfoLike[]): DeviceInfoLike | null {
  const rear = devices.filter((d) => d.kind === 'videoinput' && BACK.test(d.label));
  return (
    rear.find((d) => ULTRA_WIDE.test(d.label)) ??
    rear.find((d) => !AVOID.test(d.label)) ??
    null
  );
}

/**
 * 'capped' asks for exactly size.fps: the max keeps iOS from picking a slower
 * format that merely comes closest, and from running faster than wanted.
 * 'relaxed' is the retry for a camera that refused that (OverconstrainedError).
 */
export type FrameRateMode = 'capped' | 'relaxed';

export function videoConstraints(
  deviceId: string | null,
  size: CaptureSize,
  mode: FrameRateMode = 'capped',
): MediaTrackConstraints {
  const common = {
    width: { ideal: size.width },
    height: { ideal: size.height },
    frameRate: mode === 'capped' ? { ideal: size.fps, max: size.fps } : { ideal: size.fps },
  };
  return deviceId === null
    ? { facingMode: { ideal: 'environment' }, ...common }
    : { deviceId: { exact: deviceId }, ...common };
}

/**
 * The camera is first opened at no more than this: every iPhone lens offers it
 * at 720p, so the open itself never fails or picks an odd format for want of a
 * higher rate. A faster rate is then asked for once the camera says it has one.
 */
export const OPENING_FPS_LIMIT = 60;

/** The size and rate to open the camera with. */
export function openingSize(size: CaptureSize): CaptureSize {
  return { ...size, fps: Math.min(size.fps, OPENING_FPS_LIMIT) };
}

/**
 * The faster rate to ask an open camera for: the most it offers
 * (getCapabilities().frameRate.max), up to the setting. Null when it is
 * already there or does not say what it can do.
 */
export function raisedFrameRate(
  settingFps: number,
  runningFps: number | undefined,
  capabilityMaxFps: number | undefined,
): number | null {
  if (!isPlausibleFps(capabilityMaxFps)) return null;
  const target = Math.min(settingFps, capabilityMaxFps);
  const current = isPlausibleFps(runningFps) ? runningFps : Math.min(settingFps, OPENING_FPS_LIMIT);
  return target > current ? target : null;
}

/** applyConstraints for raising the rate: the same size, and the new rate as a preference, not a demand. */
export function raiseConstraints(size: CaptureSize, fps: number): MediaTrackConstraints {
  return { width: { ideal: size.width }, height: { ideal: size.height }, frameRate: { ideal: fps } };
}

/** Whether getUserMedia failed because the camera cannot meet the constraints (not, say, permission). */
export function isOverconstrained(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'OverconstrainedError';
}

/** Fit the video into maxSide on its long edge, keeping the aspect ratio. */
export function scaledSize(width: number, height: number, maxSide: number): { w: number; h: number } {
  const scale = Math.min(1, maxSide / Math.max(width, height, 1));
  return { w: Math.max(1, Math.round(width * scale)), h: Math.max(1, Math.round(height * scale)) };
}

export interface FrameMetadataLike {
  readonly captureTime?: number;
  readonly expectedDisplayTime?: number;
}

/**
 * Best estimate of when a frame was captured, in ms since the Unix epoch, on the
 * same wall clock (Date.now) the clock-offset pings use. requestVideoFrameCallback
 * times are on the monotonic clock; see monotonicToWallMs. A display time in the
 * future (the next vsync) counts as no delay.
 */
export function frameTimeMs(
  metadata: FrameMetadataLike | undefined,
  wallNowMs: number,
  monotonicNowMs: number,
): number {
  return monotonicToWallMs(metadata?.captureTime ?? metadata?.expectedDisplayTime, wallNowMs, monotonicNowMs);
}

/** HTMLMediaElement.HAVE_CURRENT_DATA: the current frame is available to draw. */
const HAVE_CURRENT_DATA = 2;

export interface FallbackFrameState {
  readonly paused: boolean;
  readonly readyState: number;
  readonly muted: boolean;
  readonly trackState: 'live' | 'ended';
}

/**
 * Without requestVideoFrameCallback, frames are grabbed on a timer, which would
 * happily re-encode the last picture of a paused or interrupted camera with a
 * fresh timestamp. Only grab while the camera is really delivering frames.
 */
export function shouldGrabFallbackFrame(state: FallbackFrameState): boolean {
  return !state.paused && state.readyState >= HAVE_CURRENT_DATA && !state.muted && state.trackState === 'live';
}
