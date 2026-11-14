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
  fresh: z.boolean(),
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
})

export type ServiceStatus = z.infer<typeof statusSchema>
export type GpsStatus = z.infer<typeof gpsSchema>

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
