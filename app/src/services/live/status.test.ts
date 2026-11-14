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

  it('treats a status without clock_synced as not synced', () => {
    const message = statusMessage()
    delete message.clock_synced
    expect(parseStatus(JSON.stringify(message))?.clock_synced).toBe(false)
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
