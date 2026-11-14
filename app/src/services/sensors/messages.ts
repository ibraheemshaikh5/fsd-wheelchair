// The iPad's sensor messages to the driving service's /ws/phone: hello (with no
// camera), ping, gps, heading and imu. Same wire format as the phone's capture
// page (web/capture/location.ts, heading.ts, motion.ts, clock.ts) and
// docs/phone-protocol.md; the service validates every message, so a drift
// between the two shows up as a refused message, never as bad data.

export const PROTOCOL_VERSION = 1 as const
/** The service's IMU rotation columns are about device x, y, z. */
export const IMU_AXES = 'device_xyz' as const

const FULL_TURN_DEG = 360

interface Base {
  readonly v: typeof PROTOCOL_VERSION
  /** iPad clock, ms since the Unix epoch. */
  readonly t: number
}

export interface HelloMessage extends Base {
  readonly type: 'hello'
  readonly user_agent: string
  /** Null: the iPad sends no frames; an external camera does. */
  readonly camera: null
  readonly mount: null
  readonly imu_axes: typeof IMU_AXES
}

export interface PingMessage extends Base {
  readonly type: 'ping'
  readonly rtt_ms: number | null
  readonly offset_ms: number | null
}

export interface GpsMessage extends Base {
  readonly type: 'gps'
  readonly lat: number
  readonly lon: number
  readonly accuracy_m: number
  readonly altitude_m: number | null
  readonly speed_mps: number | null
  readonly course_deg: number | null
  readonly fix_t: number
}

export interface HeadingMessage extends Base {
  readonly type: 'heading'
  readonly magnetic_deg: number
  readonly accuracy_deg: number | null
}

/** [ms since message t, accel x, y, z (m/s², with gravity), rotation rate about x, y, z (deg/s)] */
export type ImuSample = readonly [number, number, number, number, number, number, number]

export interface ImuMessage extends Base {
  readonly type: 'imu'
  readonly samples: readonly ImuSample[]
}

export type SensorMessage = HelloMessage | PingMessage | GpsMessage | HeadingMessage | ImuMessage

export interface ClockSample {
  readonly rtt_ms: number
  /** server_time = ipad_time + offset_ms */
  readonly offset_ms: number
}

const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
// Safari reports speed and heading as null, NaN or -1 when it has no value.
const nonNegativeOrNull = (value: number | null): number | null =>
  value !== null && Number.isFinite(value) && value >= 0 ? value : null

export function helloMessage(nowMs: number, userAgent: string): HelloMessage {
  return {
    v: PROTOCOL_VERSION,
    type: 'hello',
    t: nowMs,
    user_agent: userAgent.slice(0, 512),
    camera: null,
    mount: null,
    imu_axes: IMU_AXES,
  }
}

export function pingMessage(nowMs: number, sample: ClockSample | null): PingMessage {
  return {
    v: PROTOCOL_VERSION,
    type: 'ping',
    t: nowMs,
    rtt_ms: sample?.rtt_ms ?? null,
    offset_ms: sample?.offset_ms ?? null,
  }
}

/** Round trip and offset from one pong, assuming equal delay each way. */
export function sampleFromPong(pong: { t: number; server_t: number }, receivedMs: number): ClockSample | null {
  const rtt = receivedMs - pong.t
  if (rtt < 0) return null
  return { rtt_ms: rtt, offset_ms: pong.server_t - (pong.t + rtt / 2) }
}

export interface PositionLike {
  readonly coords: {
    readonly latitude: number
    readonly longitude: number
    readonly accuracy: number
    readonly altitude: number | null
    readonly speed: number | null
    readonly heading: number | null
  }
  readonly timestamp: number
}

export function gpsMessage(position: PositionLike, nowMs: number): GpsMessage {
  const { coords } = position
  const course = nonNegativeOrNull(coords.heading)
  return {
    v: PROTOCOL_VERSION,
    type: 'gps',
    t: nowMs,
    lat: coords.latitude,
    lon: coords.longitude,
    accuracy_m: coords.accuracy,
    altitude_m: isFiniteNumber(coords.altitude) ? coords.altitude : null,
    speed_mps: nonNegativeOrNull(coords.speed),
    course_deg: course === null ? null : course % FULL_TURN_DEG,
    fix_t: position.timestamp > 0 ? position.timestamp : nowMs,
  }
}

/**
 * The last fix, restamped to resend after a reconnect, or null if too old. The
 * service ages a fix by fix_t, so a resend never makes it look newer.
 */
export function recentFix(fix: GpsMessage | null, nowMs: number, maxAgeMs: number): GpsMessage | null {
  if (fix === null || nowMs - fix.fix_t > maxAgeMs) return null
  return { ...fix, t: nowMs }
}

export interface OrientationLike {
  readonly webkitCompassHeading?: number
  readonly webkitCompassAccuracy?: number
  readonly alpha: number | null
  readonly absolute: boolean
}

/** Safari's compass heading (clockwise from magnetic north), or an absolute alpha elsewhere. */
export function headingMessage(event: OrientationLike, nowMs: number): HeadingMessage | null {
  const compass = event.webkitCompassHeading
  if (isFiniteNumber(compass) && compass >= 0) {
    const accuracy = event.webkitCompassAccuracy
    return {
      v: PROTOCOL_VERSION,
      type: 'heading',
      t: nowMs,
      magnetic_deg: compass % FULL_TURN_DEG,
      // iOS reports -1 when the compass needs calibrating.
      accuracy_deg: isFiniteNumber(accuracy) && accuracy >= 0 ? accuracy : null,
    }
  }
  if (event.absolute && isFiniteNumber(event.alpha)) {
    const deg = (FULL_TURN_DEG - event.alpha) % FULL_TURN_DEG
    return { v: PROTOCOL_VERSION, type: 'heading', t: nowMs, magnetic_deg: deg, accuracy_deg: null }
  }
  return null
}

interface Vector3Like {
  readonly x: number | null
  readonly y: number | null
  readonly z: number | null
}

export interface MotionLike {
  readonly accelerationIncludingGravity: Vector3Like | null
  readonly rotationRate: { readonly alpha: number | null; readonly beta: number | null; readonly gamma: number | null } | null
}

export interface ImuBatch {
  /** iPad time of the first sample; sample times are relative to it. */
  readonly startMs: number | null
  readonly samples: readonly ImuSample[]
}

export const EMPTY_BATCH: ImuBatch = Object.freeze({ startMs: null, samples: Object.freeze([]) })

const orZero = (value: number | null | undefined): number => value ?? 0

/**
 * Add one motion event read at sampleMs. WebKit fills rotationRate.alpha with
 * the rate about device x, beta about y and gamma about z, sent in that order.
 */
export function addMotion(batch: ImuBatch, event: MotionLike, sampleMs: number): ImuBatch {
  const accel = event.accelerationIncludingGravity
  if (accel === null) return batch
  const startMs = batch.startMs ?? sampleMs
  const previousDt = batch.samples.at(-1)?.[0] ?? 0
  const rotation = event.rotationRate
  const sample: ImuSample = [
    Math.max(previousDt, sampleMs - startMs),
    orZero(accel.x),
    orZero(accel.y),
    orZero(accel.z),
    orZero(rotation?.alpha),
    orZero(rotation?.beta),
    orZero(rotation?.gamma),
  ]
  return { startMs, samples: [...batch.samples, sample] }
}

export interface BatchResult {
  readonly message: ImuMessage | null
  readonly batch: ImuBatch
}

/** Emit the batch once it is old enough or full; otherwise keep collecting. */
export function takeBatch(batch: ImuBatch, nowMs: number, flushMs: number, maxSamples: number): BatchResult {
  if (batch.startMs === null || batch.samples.length === 0) return { message: null, batch }
  const due = nowMs - batch.startMs >= flushMs || batch.samples.length >= maxSamples
  if (!due) return { message: null, batch }
  return {
    message: { v: PROTOCOL_VERSION, type: 'imu', t: batch.startMs, samples: batch.samples },
    batch: EMPTY_BATCH,
  }
}
