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

export function videoConstraints(deviceId: string | null, size: CaptureSize): MediaTrackConstraints {
  const common = {
    width: { ideal: size.width },
    height: { ideal: size.height },
    frameRate: { ideal: size.fps },
  };
  return deviceId === null
    ? { facingMode: { ideal: 'environment' }, ...common }
    : { deviceId: { exact: deviceId }, ...common };
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
 * same wall clock (Date.now) the clock-offset pings use.
 *
 * requestVideoFrameCallback times are on the monotonic clock (performance.now).
 * timeOrigin + performance.now() may drift from Date.now(): on iOS the monotonic
 * clock stops while the phone sleeps, and the wall clock can be stepped by NTP.
 * So only the short delay since the frame (measured on the monotonic clock) is
 * taken from it, and subtracted from the wall clock now. A display time in the
 * future (the next vsync) counts as no delay, so a frame is never stamped later
 * than now.
 */
export function frameTimeMs(
  metadata: FrameMetadataLike | undefined,
  wallNowMs: number,
  monotonicNowMs: number,
): number {
  const relative = metadata?.captureTime ?? metadata?.expectedDisplayTime;
  if (relative === undefined) return wallNowMs;
  return wallNowMs - Math.max(0, monotonicNowMs - relative);
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
