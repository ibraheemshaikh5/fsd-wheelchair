import { z } from 'zod'

// The part of the driving service's status (GET /api/status and every /ws/viewer
// text message) that the iPad uses. Mirrors service/drive_service/status.py.
// Unknown fields are allowed so the service can add to the status freely; a
// message missing anything below is ignored rather than half-applied.

const finite = z.number().finite()
const nullableFinite = finite.nullable()

const gpsSchema = z.object({
  lat: finite.min(-90).max(90),
  lon: finite.min(-180).max(180),
  accuracy_m: finite.nonnegative(),
  speed_mps: nullableFinite,
  course_deg: nullableFinite,
  fresh: z.boolean(),
})

const headingSchema = z.object({
  /** Best heading, clockwise, in `reference` north. */
  deg: nullableFinite,
  reference: z.enum(['true', 'magnetic']).nullable(),
})

const frameSchema = z.object({
  seq: z.number().int(),
  /** The newest frame's age since capture; null before clock sync. Absent from older services. */
  age_ms: finite.nonnegative().nullable().optional().default(null),
  fresh: z.boolean(),
})

/**
 * What the service derives from the phone's IMU (fused with GPS). Signs:
 * yaw positive = turning left; accel_fwd positive = speeding up along the
 * camera's forward direction; accel_lat positive = toward the chair's left.
 */
const motionSchema = z.object({
  speed_mps: finite.nonnegative().nullable(),
  speed_source: z.enum(['gps', 'imu', 'still']).nullable(),
  yaw_rate_dps: nullableFinite,
  accel_fwd_mps2: nullableFinite,
  accel_lat_mps2: nullableFinite,
  age_ms: finite.nonnegative().nullable(),
  /** Newest IMU sample at most 250 ms old, after clock sync. */
  fresh: z.boolean(),
})

const frameSourceSchema = z.object({
  kind: z.enum(['phone', 'external']),
  connected: z.boolean(),
  clock_synced: z.boolean(),
})

const statusSchema = z.object({
  type: z.literal('status'),
  phone_connected: z.boolean(),
  // Older services did not send this; treat it as not synced (fail closed).
  clock_synced: z.boolean().optional().default(false),
  sensors_ok: z.boolean(),
  frame: frameSchema.nullable(),
  gps: gpsSchema.nullable(),
  heading: headingSchema.nullable(),
  stats: z.object({ fps: finite.nonnegative() }),
  // Where the service thinks the rider is going; absent from older services.
  destination: z.object({ lat: finite, lon: finite }).nullable().optional().default(null),
  // Absent from older services. A malformed block only loses the motion readings:
  // rejecting the whole status would make the link look dead.
  motion: motionSchema.nullable().default(null).catch(null),
  // Where frames come from: the phone, or an external camera (USB on the Mac, later
  // the chair's board) with this iPad sending GPS, compass and motion. Absent from
  // older services, which only had the phone.
  frame_source: frameSourceSchema.nullable().optional().default(null).catch(null),
})

export type ServiceStatus = z.infer<typeof statusSchema>
export type GpsStatus = z.infer<typeof gpsSchema>
export type MotionStatus = z.infer<typeof motionSchema>

/** A status message, or null for anything that is not one (pongs, malformed JSON, an older shape). */
export function parseStatus(raw: string): ServiceStatus | null {
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return null
  }
  const parsed = statusSchema.safeParse(value)
  return parsed.success ? parsed.data : null
}
