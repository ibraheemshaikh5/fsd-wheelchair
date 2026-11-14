import { type ImuMessage, type ImuSample, PROTOCOL_VERSION } from '../shared/protocol';
import { monotonicToWallMs } from './clock';

interface Vector3Like {
  readonly x: number | null;
  readonly y: number | null;
  readonly z: number | null;
}

interface RotationLike {
  readonly alpha: number | null;
  readonly beta: number | null;
  readonly gamma: number | null;
}

/** The parts of a DeviceMotionEvent this uses. */
export interface MotionLike {
  readonly accelerationIncludingGravity: Vector3Like | null;
  readonly rotationRate: RotationLike | null;
  /** When the sensor reading was taken, on the monotonic clock (performance.now). */
  readonly timeStamp?: number;
}

/**
 * Sent in hello so the service knows the rotation columns are about device
 * x, y, z. Recordings from before this marker hold [about y, about z, about x].
 */
export const IMU_AXES = 'device_xyz' as const;

/** An event time this much older than its handler is not on the monotonic clock at all. */
const MAX_EVENT_DELAY_MS = 1000;

export interface ImuBatch {
  /** Phone time of the first sample; sample times are relative to it. */
  readonly startMs: number | null;
  readonly samples: readonly ImuSample[];
}

export const EMPTY_BATCH: ImuBatch = Object.freeze({ startMs: null, samples: Object.freeze([]) });

const orZero = (value: number | null | undefined): number => value ?? 0;

/** When the motion event's reading was taken, on the wall clock; now when the event has no usable time. */
export function motionTimeMs(event: Pick<MotionLike, 'timeStamp'>, wallNowMs: number, monotonicNowMs: number): number {
  return monotonicToWallMs(event.timeStamp, wallNowMs, monotonicNowMs, MAX_EVENT_DELAY_MS);
}

/**
 * Add one motion event, read at sampleMs (wall clock), to the batch.
 *
 * Axes (device frame: x to the right of the screen, y toward its top, z out of
 * the screen): acceleration x, y, z as the browser gives them, then rotation
 * rate about x, y, z. WebKit on iOS fills rotationRate.alpha with the rate about
 * x, beta about y and gamma about z (not the W3C spec's z, x, y), so they are
 * sent in that order. Sample times never run backwards within a batch.
 */
export function addMotion(batch: ImuBatch, event: MotionLike, sampleMs: number): ImuBatch {
  const accel = event.accelerationIncludingGravity;
  if (accel === null) return batch;
  const startMs = batch.startMs ?? sampleMs;
  const previousDt = batch.samples.at(-1)?.[0] ?? 0;
  const rotation = event.rotationRate;
  const sample: ImuSample = [
    Math.max(previousDt, sampleMs - startMs),
    orZero(accel.x),
    orZero(accel.y),
    orZero(accel.z),
    orZero(rotation?.alpha),
    orZero(rotation?.beta),
    orZero(rotation?.gamma),
  ];
  return { startMs, samples: [...batch.samples, sample] };
}

export interface BatchResult {
  readonly message: ImuMessage | null;
  readonly batch: ImuBatch;
}

/** Emit whatever the batch holds now, however young. */
export function drainBatch(batch: ImuBatch): BatchResult {
  if (batch.startMs === null || batch.samples.length === 0) return { message: null, batch };
  return {
    message: { v: PROTOCOL_VERSION, type: 'imu', t: batch.startMs, samples: batch.samples },
    batch: EMPTY_BATCH,
  };
}

/** Emit the batch as a message once it is old enough or full; otherwise keep collecting. */
export function takeBatch(batch: ImuBatch, nowMs: number, flushMs: number, maxSamples: number): BatchResult {
  if (batch.startMs === null) return { message: null, batch };
  const due = nowMs - batch.startMs >= flushMs || batch.samples.length >= maxSamples;
  return due ? drainBatch(batch) : { message: null, batch };
}
