// Frame-rate decisions for the capture loop: what rate the camera really runs
// at, and whether to thin its frames down to the configured rate.

/** Highest rate believed from a camera, and the most the service accepts (CameraInfo.fps). */
export const MAX_CAMERA_FPS = 240;
/**
 * The default: the top rate of the iPhone's normal video formats, and three
 * frames per 20 Hz model step. Faster rates come from slow-motion formats with
 * shorter exposures (darker, noisier) and more heat, so they are opt-in.
 */
export const DEFAULT_CAPTURE_FPS = 60;
/** What a camera that does not report its rate is assumed to run at: the usual getUserMedia default. */
const ASSUMED_CAMERA_FPS = 30;
const MS_PER_SECOND = 1000;
/**
 * A frame may arrive this fraction of an interval before it is due and still be
 * taken, so callback jitter does not push a frame onto the next camera tick.
 * Well under half an interval, so a frame a whole camera tick early is skipped.
 */
const EARLY_FRAME_TOLERANCE = 0.1;

/** Whether a camera-reported rate is one to believe. */
export function isPlausibleFps(fps: number | undefined): fps is number {
  return fps !== undefined && Number.isFinite(fps) && fps > 0 && fps <= MAX_CAMERA_FPS;
}

/**
 * The rate the camera track reports it runs at. When it reports none, assume
 * the usual default (or the request, if lower) rather than the request: a
 * request of 240 says nothing about what the camera managed.
 */
export function achievedFps(reported: number | undefined, requested: number): number {
  return isPlausibleFps(reported) ? reported : Math.min(requested, ASSUMED_CAMERA_FPS);
}

/**
 * Minimum spacing between frames taken from the camera, or null to take every
 * frame it delivers. Thinning only applies when the camera runs faster than the
 * target: throttling at the camera's own rate would drop frames to jitter.
 */
export function frameIntervalMs(targetFps: number, cameraFps: number): number | null {
  return targetFps >= cameraFps ? null : MS_PER_SECOND / targetFps;
}

/** Whether a frame arriving at nowMs should be taken, given when the next one is due (null: none taken yet). */
export function isFrameDue(dueMs: number | null, nowMs: number, intervalMs: number | null): boolean {
  return intervalMs === null || dueMs === null || nowMs >= dueMs - intervalMs * EARLY_FRAME_TOLERANCE;
}

/**
 * When the frame after one taken at nowMs is due. It keeps to a fixed schedule
 * rather than restarting from the arrival time, so a target that does not divide
 * the camera rate (60 -> 50, 24 -> 20) still averages the target instead of
 * dropping to every other camera frame. After a gap it catches up by at most
 * one frame rather than sending a burst.
 */
export function nextDueMs(dueMs: number | null, nowMs: number, intervalMs: number): number {
  const base = dueMs === null ? nowMs : Math.max(dueMs, nowMs - intervalMs);
  return base + intervalMs;
}
