// Adaptive JPEG quality: on a link that cannot carry the camera's frames,
// smaller JPEGs let more of them through. Judged once a second; resolution
// never changes here.
//
// The signal is how long acks take and how many frames get through, not the
// share of frames held back: at 60 fps with 2 frames in flight, any round trip
// above ~33 ms holds back most frames even though 20+ fresh frames a second
// still arrive, which is all the driving model needs.

/** Quality never adapts below this (unless the setting itself is lower). */
export const QUALITY_FLOOR = 0.4;
/** Acks slower than this mean frames wait in the link: step down. */
export const RTT_HIGH_MS = 150;
/**
 * Acks faster than this (with enough frames getting through) mean room to step
 * up. Two frames in flight carry 20 fps up to a ~100 ms round trip, so stepping
 * up only well below that keeps a link near the limit from bouncing.
 */
export const RTT_LOW_MS = 75;
/** The driving model's frame rate: fewer frames a second getting through means step down. */
export const MIN_SENT_FPS = 20;
/**
 * Share of MIN_SENT_FPS that counts as getting through. With the camera thinned
 * to 20 fps, a window holds 19-21 frames by timing alone, and one frame lost to
 * jitter is not the link's fault.
 */
const ENOUGH_SHARE = 0.9;
const ENOUGH_FPS = MIN_SENT_FPS * ENOUGH_SHARE;
const STEP_DOWN = 0.1;
const STEP_UP = 0.05;

export interface QualityState {
  /** The JPEG quality to encode the next frame with. */
  readonly quality: number;
  /** The configured setting: the most quality ever goes back up to. */
  readonly ceiling: number;
}

/** What happened on the link over one window. */
export interface QualityWindow {
  readonly seconds: number;
  /** Frames the camera delivered. */
  readonly captured: number;
  /** Frames sent. */
  readonly sent: number;
  /** Smoothed ack round trip, or null before this connection's first ack. */
  readonly ackRttMs: number | null;
}

type Step = 'down' | 'up' | 'hold';

export const initialQuality = (configured: number): QualityState => ({ quality: configured, ceiling: configured });

/** Rounded to hundredths, so repeated steps do not drift (0.7 - 0.1 - 0.1 stays 0.5). */
const round = (value: number): number => Math.round(value * 100) / 100;

function step(window: QualityWindow, ackRttMs: number): Step {
  const capturedFps = window.captured / window.seconds;
  const sentFps = window.sent / window.seconds;
  // A camera delivering under 20 fps (give or take jitter) is not the link's fault.
  const starved = capturedFps >= ENOUGH_FPS && sentFps < ENOUGH_FPS;
  if (ackRttMs > RTT_HIGH_MS || starved) return 'down';
  return ackRttMs < RTT_LOW_MS ? 'up' : 'hold';
}

/** Step quality for one window. A window with no frames or no ack yet says nothing about the link. */
export function adjustQuality(state: QualityState, window: QualityWindow): QualityState {
  if (window.captured <= 0 || window.seconds <= 0 || window.ackRttMs === null) return state;
  const direction = step(window, window.ackRttMs);
  if (direction === 'down') {
    const floor = Math.min(QUALITY_FLOOR, state.ceiling);
    return { ...state, quality: Math.max(floor, round(state.quality - STEP_DOWN)) };
  }
  if (direction === 'up') return { ...state, quality: Math.min(state.ceiling, round(state.quality + STEP_UP)) };
  return state;
}
