import { type ImuMessage, type ImuSample, PROTOCOL_VERSION } from '../shared/protocol';

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
}

export interface ImuBatch {
  /** Phone time of the first sample; sample times are relative to it. */
  readonly startMs: number | null;
  readonly samples: readonly ImuSample[];
}

export const EMPTY_BATCH: ImuBatch = Object.freeze({ startMs: null, samples: Object.freeze([]) });

const orZero = (value: number | null | undefined): number => value ?? 0;

/**
 * Add one motion event to the batch. The browser's rotation axes are renamed to
 * match the acceleration axes: beta is about x, gamma about y, alpha about z.
 */
export function addMotion(batch: ImuBatch, event: MotionLike, nowMs: number): ImuBatch {
  const accel = event.accelerationIncludingGravity;
  if (accel === null) return batch;
  const startMs = batch.startMs ?? nowMs;
  const rotation = event.rotationRate;
  const sample: ImuSample = [
    nowMs - startMs,
    orZero(accel.x),
    orZero(accel.y),
    orZero(accel.z),
    orZero(rotation?.beta),
    orZero(rotation?.gamma),
    orZero(rotation?.alpha),
  ];
  return { startMs, samples: [...batch.samples, sample] };
}

export interface BatchResult {
  readonly message: ImuMessage | null;
  readonly batch: ImuBatch;
}

/** Emit the batch as a message once it is old enough or full; otherwise keep collecting. */
export function takeBatch(batch: ImuBatch, nowMs: number, flushMs: number, maxSamples: number): BatchResult {
  if (batch.startMs === null || batch.samples.length === 0) return { message: null, batch };
  const due = nowMs - batch.startMs >= flushMs || batch.samples.length >= maxSamples;
  if (!due) return { message: null, batch };
  return {
    message: { v: PROTOCOL_VERSION, type: 'imu', t: batch.startMs, samples: batch.samples },
    batch: EMPTY_BATCH,
  };
}
