import { lazy, Suspense, useEffect, useState } from 'react'
import { hasGoogleKey, onGoogleFailure } from '../../lib/googleMaps'
import type { MapProps } from './shared'

// Only one of the two is ever shown, and MapLibre is large, so load on demand.
const GoogleMap = lazy(() => import('./GoogleMap').then((m) => ({ default: m.GoogleMap })))
const MapLibreMap = lazy(() => import('./MapLibreMap').then((m) => ({ default: m.MapLibreMap })))

/** The map, on Google when a key is configured and working, otherwise on keyless tiles. */
export function MapPane(props: Omit<MapProps, 'satellite'>) {
  const [useGoogle, setUseGoogle] = useState(hasGoogleKey)
  const [satellite, setSatellite] = useState(false)
  useEffect(() => onGoogleFailure(() => setUseGoogle(false)), [])
  const Map = useGoogle ? GoogleMap : MapLibreMap

  return (
    <>
      <Suspense fallback={null}>
        <Map {...props} satellite={satellite} />
      </Suspense>
      <button
        type="button"
        className="map-style-btn"
        aria-pressed={satellite}
        onClick={() => setSatellite((on) => !on)}
      >
        {satellite ? 'Map' : 'Satellite'}
      </button>
    </>
  )
}
