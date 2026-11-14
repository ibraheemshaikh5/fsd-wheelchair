import { mpsToMph } from '../../lib/format'

const RADIUS = 66
const CENTRE = 80
const START_DEG = 150 // lower left, measured clockwise from 3 o'clock
const SWEEP_DEG = 240

function pointAt(deg: number): string {
  const rad = ((START_DEG + deg) * Math.PI) / 180
  return `${(CENTRE + RADIUS * Math.cos(rad)).toFixed(2)} ${(CENTRE + RADIUS * Math.sin(rad)).toFixed(2)}`
}

const arc = (deg: number) => `M${pointAt(0)} A${RADIUS} ${RADIUS} 0 ${deg > 180 ? 1 : 0} 1 ${pointAt(deg)}`

interface Props {
  speedMps: number
  maxMps: number
  /** Self-driving is engaged, so the arc takes the accent colour. */
  active: boolean
}

/** Speed as a number inside an arc that fills towards the chair's top speed. */
export function SpeedArc({ speedMps, maxMps, active }: Props) {
  const mph = mpsToMph(speedMps).toFixed(1)
  const fraction = Math.max(0, Math.min(1, maxMps > 0 ? speedMps / maxMps : 0))
  return (
    <div className="speed-arc" role="img" aria-label={`${mph} miles per hour`}>
      <svg viewBox="0 0 160 150" width="160" height="150" aria-hidden="true">
        <path d={arc(SWEEP_DEG)} className="speed-track" />
        {fraction > 0.005 && <path d={arc(SWEEP_DEG * fraction)} className={`speed-fill ${active ? 'active' : ''}`} />}
      </svg>
      <strong>{mph}</strong>
      <span>MPH</span>
      <em>max {mpsToMph(maxMps).toFixed(0)}</em>
    </div>
  )
}
