import { useCallback, useState } from 'react'
import type { Place } from './types'
import { createDrivingService } from './services/drivingService'
import { useDriveState } from './hooks/useDriveState'
import { Clock } from './components/Clock'
import { DrivePane } from './components/drive/DrivePane'
import { TripCard, TurnCard } from './components/drive/TripCards'
import { PermissionsGate } from './components/PermissionsGate'
import { MapPane } from './components/map/MapPane'
import { SearchPanel } from './components/search/SearchPanel'

const service = createDrivingService()

export default function App() {
  const state = useDriveState(service)
  const [results, setResults] = useState<Place[]>([])
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [routeError, setRouteError] = useState<string | null>(null)
  const active = state.mode !== 'idle'
  // Indoors there is no map route, so the chair view takes the whole screen.
  const indoor = state.languageGoal !== null

  const select = useCallback(async (place: Place) => {
    setPendingId(place.id)
    setRouteError(null)
    try {
      await service.setDestination(place)
      setResults([])
    } catch {
      setRouteError(`Couldn’t find a route to ${place.name}.`)
    } finally {
      setPendingId(null)
    }
  }, [])

  const goIndoors = useCallback((instruction: string) => {
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
            />
          )}
        </div>
      </main>

      <PermissionsGate />
    </div>
  )
}
