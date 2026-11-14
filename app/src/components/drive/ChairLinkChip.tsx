import type { DriveState } from '../../types'
import { PhoneIcon } from '../icons'

type Level = 'ok' | 'warn' | 'bad' | 'sim'

interface LinkSummary {
  level: Level
  label: string
  detail: string | null
  /** What a screen reader says for the whole chip. */
  spoken: string
}

const FEET_PER_METRE = 3.28084

/**
 * One glanceable line on whether the iPad is hearing the chair's sensors: the
 * phone, or (with an external camera) that camera plus this iPad's own GPS,
 * compass and motion.
 */
export function linkSummary(state: DriveState): LinkSummary {
  if (state.source === 'simulated') {
    return { level: 'sim', label: 'Simulator', detail: null, spoken: 'Simulated chair, no phone' }
  }
  const { link } = state
  if (link.service === 'connecting') {
    return { level: 'warn', label: 'Connecting…', detail: null, spoken: 'Connecting to the driving service' }
  }
  if (link.service === 'offline') {
    return { level: 'bad', label: 'Service offline', detail: null, spoken: 'Driving service offline' }
  }
  const external = link.frameSource === 'external'
  if (external && !link.cameraConnected) {
    return { level: 'bad', label: 'No camera', detail: null, spoken: 'Chair camera not connected' }
  }
  const label = external ? 'iPad' : 'Phone'
  const device = external ? 'iPad sensors' : 'Chair phone'
  if (link.phone === 'none') {
    return { level: 'bad', label: external ? 'No iPad sensors' : 'No phone', detail: null, spoken: `${device} not connected` }
  }
  if (link.phone === 'waiting') {
    return { level: 'warn', label, detail: 'waiting', spoken: `${device} connected, waiting for fresh readings` }
  }
  if (link.gpsAccuracyM === null) return { level: 'ok', label, detail: null, spoken: `${device} streaming` }
  const feet = Math.round(link.gpsAccuracyM * FEET_PER_METRE)
  return { level: 'ok', label, detail: `±${feet} ft`, spoken: `${device} streaming, GPS within ${feet} feet` }
}

export function ChairLinkChip({ state }: { state: DriveState }) {
  const { level, label, detail, spoken } = linkSummary(state)
  return (
    <span className={`link-chip ${level}`} role="status" aria-label={spoken}>
      <PhoneIcon />
      <span>{label}</span>
      {detail && <span className="link-detail">{detail}</span>}
    </span>
  )
}
