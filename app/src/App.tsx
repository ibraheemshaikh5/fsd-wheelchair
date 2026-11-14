import { useCallback, useRef, useState } from 'react'
import type { Place } from './types'
import { createDrivingService } from './services/drivingService'
import { ServiceRefusedError } from './services/errors'
import { useDriveState } from './hooks/useDriveState'
import { useSensorUplink } from './hooks/useSensorUplink'
import { Clock } from './components/Clock'
import { DrivePane } from './components/drive/DrivePane'
import { TripCard, TurnCard } from './components/drive/TripCards'
import { PermissionsGate } from './components/PermissionsGate'
import { SensorUplinkNotice } from './components/SensorUplinkNotice'
import { MapPane } from './components/map/MapPane'
import { SearchPanel } from './components/search/SearchPanel'

const service = createDrivingService()
// Fast Refresh re-runs this module on edit; close the old service's socket so
// leaked viewer sockets never use up the service's viewer limit.
import.meta.hot?.dispose(() => service.dispose?.())

export default function App() {
  const state = useDriveState(service)
  // With an external camera on the chair, this iPad sends GPS, compass and motion.
  const uplink = useSensorUplink(state.source === 'live' && state.link.frameSource === 'external')
  const [results, setResults] = useState<Place[]>([])
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [routeError, setRouteError] = useState<string | null>(null)
  // Numbers each destination request, so an older one finishing late can't
  // clear the spinner or post an error for the one the rider made since.
  const selectSeq = useRef(0)
  const active = state.mode !== 'idle'
  // Indoors there is no map route, so the chair view takes the whole screen.
  const indoor = state.languageGoal !== null

  const select = useCallback(async (place: Place) => {
    const req = ++selectSeq.current
    const isLatest = () => req === selectSeq.current
    setPendingId(place.id)
    setRouteError(null)
    try {
      await service.setDestination(place)
      if (isLatest()) setResults([])
    } catch (error) {
      if (!isLatest()) return
      // The service explains its own refusals; anything else is a routing failure.
      setRouteError(
        error instanceof ServiceRefusedError
          ? error.message
          : `Couldn’t find a route to ${place.name}.`,
      )
    } finally {
      if (isLatest()) setPendingId(null)
    }
  }, [])

  const clearRouteError = useCallback(() => setRouteError(null), [])

  const goIndoors = useCallback((instruction: string) => {
    // An outdoor route still being planned is superseded by this goal.
    selectSeq.current++
    setPendingId(null)
    setRouteError(null)
    setResults([])
    void service.setLanguageGoal(instruction)
  }, [])

  return (
    <div className={`app ${indoor ? 'indoor' : ''}`}>
      <aside className="drive-pane">
        <DrivePane state={state} service={service} wide={indoor} />
      </aside>

      <main className="map-pane" aria-hidden={indoor}>
        <MapPane state={state} results={results} onSelect={select} />
        <Clock />
        <div className="map-overlay">
          {active ? (
            <>
              <TurnCard state={state} />
              <span className="spacer" />
              <TripCard state={state} service={service} />
            </>
          ) : (
            <SearchPanel
              origin={state.position}
              onResults={setResults}
              onSelect={select}
              onIndoorGoal={goIndoors}
              pendingId={pendingId}
              error={routeError}
              onEdit={clearRouteError}
            />
          )}
        </div>
      </main>

      <PermissionsGate />
      <SensorUplinkNotice uplink={uplink} />
    </div>
  )
}
