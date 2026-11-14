import { useState } from 'react'
import type { DriveMode, DriveState } from '../../types'
import type { DrivingService } from '../../services/drivingService'
import { useWakeLock } from '../../hooks/useWakeLock'
import { Button } from '../Button'
import { BatteryIcon, CameraIcon, SteeringIcon } from '../icons'
import { CameraInset } from './CameraInset'
import { ChairLinkChip } from './ChairLinkChip'
import { DriveView } from './DriveView'
import { SpeedArc } from './SpeedArc'
import { TripCard } from './TripCards'

const MODE_LABEL: Record<DriveMode, string> = {
  idle: 'Parked',
  ready: 'Parked',
  arrived: 'Parked',
  driving: 'Self-driving',
  manual: 'Manual',
}

/** What the rider must act on right now, if anything. */
function alertFor(state: DriveState): { title: string; detail: string } | null {
  if (!state.connected) {
    // The live service never drives yet, so losing it cannot have stopped the chair.
    const detail =
      state.source === 'live'
        ? `The iPad can’t reach the driving service${state.link.issue ? ` (${state.link.issue})` : ''}. Reconnecting…`
        : 'The chair has stopped. Use the joystick.'
    return { title: 'Connection lost', detail }
  }
  if (state.takeOver) return { title: 'Take over now', detail: state.takeOver }
  return null
}

interface Props {
  state: DriveState
  service: DrivingService
  /** Indoor trips have no map, so the pane fills the screen and carries the trip card. */
  wide: boolean
}

export function DrivePane({ state, service, wide }: Props) {
  const [cameraOpen, setCameraOpen] = useState(false)
  const { mode } = state
  const alert = alertFor(state)
  // Keep the screen on from the moment a trip is set until it is cleared.
  useWakeLock(mode !== 'idle')

  return (
    <div className={`drive-pane-inner ${alert ? 'alerting' : ''}`}>
      <DriveView state={state} wide={wide} />

      <header className="drive-status">
        <span className={`mode-chip ${mode}`}>
          <i />
          {MODE_LABEL[mode]}
        </span>
        <ChairLinkChip state={state} />
        <span className="spacer" />
        <button
          type="button"
          className={`icon-btn camera-btn ${cameraOpen ? 'on' : ''}`}
          aria-label={cameraOpen ? 'Hide camera' : 'Show camera'}
          aria-pressed={cameraOpen}
          onClick={() => setCameraOpen((open) => !open)}
        >
          <CameraIcon />
        </button>
        {state.batteryPct !== null && (
          <span className="battery">
            {Math.round(state.batteryPct)}% <BatteryIcon pct={state.batteryPct} />
          </span>
        )}
      </header>

      <div className={`drive-top ${cameraOpen ? 'with-camera' : ''}`}>
        <SpeedArc speedMps={state.speedMps} maxMps={state.maxSpeedMps} active={mode === 'driving'} />
        {state.reason && !alert && (
          <p className={`drive-reason ${mode === 'driving' ? state.pathStatus : ''}`} aria-live="polite">
            {state.reason}
          </p>
        )}
        {cameraOpen && <CameraInset service={service} />}
      </div>

      {alert && (
        <div className="takeover" role="alert">
          <strong>{alert.title}</strong>
          <span>{alert.detail}</span>
        </div>
      )}

      <div className="drive-bottom">
        <div className="drive-action">
          <DriveControl state={state} service={service} />
        </div>
        {wide && <TripCard state={state} service={service} />}
      </div>
    </div>
  )
}

/** The one large control at the bottom of the pane; what it does depends on the mode. */
function DriveControl({ state, service }: { state: DriveState; service: DrivingService }) {
  const hasTrip = Boolean(state.route || state.languageGoal)
  switch (state.mode) {
    case 'idle':
      return <div className="hold-note">Parked · Choose a destination</div>
    case 'arrived':
      return <div className="hold-note">{state.source === 'live' ? 'Arrived' : 'Arrived · Holding'}</div>
    case 'driving':
      return (
        <Button variant="stop" size="xl" onClick={() => service.disengage()}>
          Stop
        </Button>
      )
    case 'ready':
    case 'manual':
      if (!hasTrip) return null
      if (state.engageBlocked) {
        return (
          <div className="engage-blocked" role="status">
            <Button variant="primary" size="xl" disabled>
              <SteeringIcon /> Start Self-Driving
            </Button>
            <p>{state.engageBlocked}</p>
          </div>
        )
      }
      return (
        <Button variant="primary" size="xl" onClick={() => service.engage()}>
          <SteeringIcon /> {state.mode === 'manual' ? 'Resume' : 'Start'} Self-Driving
        </Button>
      )
  }
}
