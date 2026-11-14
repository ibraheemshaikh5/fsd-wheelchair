import { describe, expect, it } from 'vitest'
import { parseStatus } from './status'
import { statusMessage } from './testing'

describe('parseStatus', () => {
  it('accepts a full status and keeps the fields the iPad uses', () => {
    const parsed = parseStatus(JSON.stringify(statusMessage()))
    expect(parsed).not.toBeNull()
    expect(parsed?.sensors_ok).toBe(true)
    expect(parsed?.gps?.lat).toBeCloseTo(38.987)
    expect(parsed?.heading?.reference).toBe('true')
    expect(parsed?.stats.fps).toBe(9.5)
  })

  it('accepts a phone with no readings yet', () => {
    const parsed = parseStatus(
      JSON.stringify(statusMessage({ phone_connected: false, sensors_ok: false, frame: null, gps: null, heading: null })),
    )
    expect(parsed?.gps).toBeNull()
  })

  it('keeps the newest frame’s age since capture, unknown when an older service leaves it out', () => {
    expect(parseStatus(JSON.stringify(statusMessage()))?.frame?.age_ms).toBe(80)
    const unknownAge = statusMessage({ frame: { seq: 7, fresh: false } })
    expect(parseStatus(JSON.stringify(unknownAge))?.frame?.age_ms).toBeNull()
    const nullAge = statusMessage({ frame: { seq: 7, age_ms: null, fresh: false } })
    expect(parseStatus(JSON.stringify(nullAge))?.frame?.age_ms).toBeNull()
  })

  it('treats a status without clock_synced as not synced', () => {
    const message = statusMessage()
    delete message.clock_synced
    expect(parseStatus(JSON.stringify(message))?.clock_synced).toBe(false)
  })

  it('keeps the motion block the service derives from the IMU', () => {
    const motion = {
      speed_mps: 1.4,
      speed_source: 'imu',
      yaw_rate_dps: 12.5,
      accel_fwd_mps2: 0.3,
      accel_lat_mps2: -0.1,
      age_ms: 40,
      fresh: true,
    }
    expect(parseStatus(JSON.stringify(statusMessage({ motion })))?.motion).toEqual(motion)
  })

  it('treats a missing or null motion block (older services) as unknown', () => {
    const message = statusMessage()
    delete message.motion
    expect(parseStatus(JSON.stringify(message))?.motion).toBeNull()
    expect(parseStatus(JSON.stringify(statusMessage({ motion: null })))?.motion).toBeNull()
  })

  it('accepts a motion block whose readings are unknown', () => {
    const motion = {
      speed_mps: null,
      speed_source: null,
      yaw_rate_dps: null,
      accel_fwd_mps2: null,
      accel_lat_mps2: null,
      age_ms: null,
      fresh: false,
    }
    expect(parseStatus(JSON.stringify(statusMessage({ motion })))?.motion).toEqual(motion)
  })

  it.each([
    ['a negative speed', { speed_mps: -1 }],
    ['an unknown speed source', { speed_source: 'radar' }],
    ['a missing fresh flag', { fresh: undefined }],
    ['a non-object', 'fast'],
  ])('keeps the status but drops a motion block with %s', (_, broken) => {
    const valid = statusMessage().motion as Record<string, unknown>
    const motion = typeof broken === 'string' ? broken : { ...valid, ...broken }
    const parsed = parseStatus(JSON.stringify(statusMessage({ motion })))
    expect(parsed).not.toBeNull()
    expect(parsed?.motion).toBeNull()
  })

  it.each([
    ['malformed JSON', '{oops'],
    ['an array', '[]'],
    ['a pong', JSON.stringify({ type: 'pong', t: 1 })],
    ['a bare status', JSON.stringify({ type: 'status' })],
    ['a latitude out of range', JSON.stringify(statusMessage({ gps: { ...(statusMessage().gps as object), lat: 91 } }))],
    ['a non-numeric speed', JSON.stringify(statusMessage({ gps: { ...(statusMessage().gps as object), speed_mps: 'fast' } }))],
    ['an unknown heading reference', JSON.stringify(statusMessage({ heading: { deg: 1, reference: 'grid' } }))],
  ])('rejects %s', (_, raw) => {
    expect(parseStatus(raw)).toBeNull()
  })
})
