import { describe, expect, it } from 'vitest'
import type { ChairLink, DriveState } from '../../types'
import { linkSummary } from './ChairLinkChip'

const LINK: ChairLink = { service: 'online', phone: 'ok', gpsAccuracyM: 4, cameraFps: 10, positionFrom: 'phone', issue: null }

const state = (link: Partial<ChairLink>, source: DriveState['source'] = 'live') =>
  ({ source, link: { ...LINK, ...link } }) as DriveState

describe('linkSummary', () => {
  it('shows the phone with its GPS accuracy in feet', () => {
    expect(linkSummary(state({}))).toMatchObject({ level: 'ok', label: 'Phone', detail: '±13 ft' })
  })

  it.each([
    [{ service: 'connecting' as const }, 'warn', 'Connecting…'],
    [{ service: 'offline' as const }, 'bad', 'Service offline'],
    [{ phone: 'none' as const }, 'bad', 'No phone'],
    [{ phone: 'waiting' as const }, 'warn', 'Phone'],
    [{ gpsAccuracyM: null }, 'ok', 'Phone'],
  ])('%o reads %s %s', (link, level, label) => {
    expect(linkSummary(state(link))).toMatchObject({ level, label })
  })

  it('says when the chair is simulated', () => {
    expect(linkSummary(state({}, 'simulated'))).toMatchObject({ level: 'sim', label: 'Simulator' })
  })
})
