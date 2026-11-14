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

/** One glanceable line on whether the iPad is hearing the chair's phone. */
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
  if (link.phone === 'none') return { level: 'bad', label: 'No phone', detail: null, spoken: 'Chair phone not connected' }
  if (link.phone === 'waiting') {
    return { level: 'warn', label: 'Phone', detail: 'waiting', spoken: 'Chair phone connected, waiting for its sensors' }
  }
  if (link.gpsAccuracyM === null) return { level: 'ok', label: 'Phone', detail: null, spoken: 'Chair phone streaming' }
  const feet = Math.round(link.gpsAccuracyM * FEET_PER_METRE)
  return { level: 'ok', label: 'Phone', detail: `±${feet} ft`, spoken: `Chair phone streaming, GPS within ${feet} feet` }
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
