import type { DriveState } from '../../types'
import type { DrivingService } from '../../services/drivingService'
import { clockTime, distanceLabel } from '../../lib/format'
import { MINUTE_MS, useNow } from '../../hooks/useNow'
import { Button } from '../Button'
import { ManeuverIcon, PinIcon } from '../icons'

/** The next manoeuvre, and the one after it. */
export function TurnCard({ state }: { state: DriveState }) {
  const { nextStep, followingStep } = state
  if (!nextStep || state.mode === 'arrived') return null
  return (
    <section className="card turn-card" aria-label="Next turn">
      <div className="turn-main">
        <ManeuverIcon kind={nextStep.step.kind} />
        <div>
          <div className="turn-distance">{distanceLabel(nextStep.distanceM)}</div>
          <div className="turn-label">{nextStep.step.label}</div>
        </div>
      </div>
      {followingStep && (
        <div className="turn-next">
          <ManeuverIcon kind={followingStep.step.kind} size={26} />
          <span className="turn-next-label">{followingStep.step.label}</span>
          <span className="turn-next-distance">{distanceLabel(followingStep.distanceM)}</span>
        </div>
      )}
    </section>
  )
}

// While moving, a once-a-second tick keeps the arrival minute from lagging the ETA.
const DRIVING_TICK_MS = 1_000

/** Where the chair is going, how long is left, and the button that ends the trip. */
export function TripCard({ state, service }: { state: DriveState; service: DrivingService }) {
  // Read from a ticking clock, so the arrival time keeps moving while the chair waits.
  const now = useNow(state.mode === 'driving' ? DRIVING_TICK_MS : MINUTE_MS)
  const title = state.destination?.name ?? state.languageGoal
  if (!title) return null
  const indoor = !state.destination
  const arrived = state.mode === 'arrived'
  return (
    <section className="card trip-card" aria-label="Trip">
      <div className="trip-destination">
        <PinIcon />
        <span>{indoor ? `“${title}”` : title}</span>
      </div>
      {arrived ? (
        <div className="trip-arrived">You have arrived</div>
      ) : indoor ? (
        <div className="trip-indoor">Indoors · no map route</div>
      ) : (
        <div className="trip-stats">
          <strong>{clockTime(new Date(now + state.etaSeconds * 1000))}</strong>
          <span>{Math.max(1, Math.round(state.etaSeconds / 60))} min</span>
          <span>{distanceLabel(state.remainingM)}</span>
        </div>
      )}
      <Button variant="quiet" size="wide" onClick={() => service.clearDestination()}>
        {arrived ? 'Done' : 'End Trip'}
      </Button>
    </section>
  )
}
