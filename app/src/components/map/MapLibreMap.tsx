import { useEffect, useRef, useState } from 'react'
import { Map as GlMap, Marker, setWorkerUrl, type GeoJSONSource, type LngLatBoundsLike } from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import type { Feature, FeatureCollection } from 'geojson'
import 'maplibre-gl/dist/maplibre-gl.css'
import { offset } from '../../lib/geo'
import { SEARCH_RADIUS_M } from '../../services/places'
import type { LatLng } from '../../types'
import { CHAIR_RED, FOLLOW_ZOOM, HOME_ZOOM, ROUTE_BLUE, type MapProps } from './shared'

// Keyless map data, used when no Google Maps key is configured: OpenFreeMap vector
// tiles (OpenStreetMap data) recoloured below, and Esri imagery for satellite.
// Esri requires the attribution shown on the map; check its terms before shipping.
const STYLE_URL = 'https://tiles.openfreemap.org/styles/positron'
const IMAGERY = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'

// MapLibre parses tiles in a worker shipped as a separate file; hand it the bundled URL.
setWorkerUrl(workerUrl)

// MapLibre zoom levels are one lower than Google's for the same scale.
const HOME = HOME_ZOOM - 1
const FOLLOW = FOLLOW_ZOOM - 1.5
// Street-map label colour. Dark enough to read in sunlight (about 5:1 on the grey land);
// labels are only coloured by the satellite effect, which also handles the street map.
const STREET_LABEL = '#5f6268'
const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] }
const CHAIR_SVG =
  '<svg viewBox="-17 -17 34 34" width="34" height="34">' +
  `<path d="M0-14 10 11 0 5-10 11Z" fill="${CHAIR_RED}" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/></svg>`

const lngLat = (p: LatLng): [number, number] => [p.lng, p.lat]

function line(points: LatLng[]): Feature {
  return { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: points.map(lngLat) } }
}

function boundsOf(points: LatLng[]): LngLatBoundsLike {
  const lats = points.map((p) => p.lat)
  const lngs = points.map((p) => p.lng)
  return [
    [Math.min(...lngs), Math.min(...lats)],
    [Math.max(...lngs), Math.max(...lats)],
  ]
}

/**
 * The in-car look: light grey land, white roads, darker grey buildings, no colour.
 * Labels and the search radius are coloured by the street/satellite effect instead.
 */
function applyTeslaLook(map: GlMap) {
  for (const layer of map.getStyle().layers) {
    const source = 'source-layer' in layer ? layer['source-layer'] : undefined
    const id = layer.id
    if (layer.type === 'background') {
      map.setPaintProperty(id, 'background-color', '#e9e9ea')
    } else if (layer.type === 'fill') {
      if (source === 'building') {
        map.setPaintProperty(id, 'fill-color', '#d9d9dc')
        map.setPaintProperty(id, 'fill-outline-color', '#cdcdd1')
        map.setPaintProperty(id, 'fill-opacity', 1)
      } else if (source === 'water') {
        map.setPaintProperty(id, 'fill-color', '#cfd0d3')
      } else if (source === 'park' || source === 'landcover') {
        map.setPaintProperty(id, 'fill-color', '#dfe0df')
      } else if (source === 'landuse') {
        map.setPaintProperty(id, 'fill-color', '#e9e9ea')
      }
    } else if (layer.type === 'line') {
      if (source === 'transportation') {
        const color = /rail/.test(id) ? '#d3d3d6' : /casing/.test(id) ? '#dededf' : '#ffffff'
        map.setPaintProperty(id, 'line-color', color)
      } else if (source === 'waterway') {
        map.setPaintProperty(id, 'line-color', '#cfd0d3')
      }
    }
  }
}

function placePin(name: string, onClick?: () => void): HTMLElement {
  const pin = document.createElement(onClick ? 'button' : 'div')
  pin.className = 'place-pin'
  const dot = document.createElement('span')
  dot.className = 'place-dot'
  const label = document.createElement('span')
  label.className = 'place-label'
  label.textContent = name
  pin.append(dot, label)
  if (onClick) pin.addEventListener('click', onClick)
  return pin
}

/** Map of the area around the chair, built on MapLibre. */
export function MapLibreMap({ state, results, onSelect, satellite }: MapProps) {
  const el = useRef<HTMLDivElement>(null)
  const [map, setMap] = useState<GlMap | null>(null)
  const chair = useRef<Marker | null>(null)
  // Re-applies the current framing after the pane changes size.
  const refit = useRef<(() => void) | null>(null)
  const { mode, position, headingDeg, route, destination } = state

  useEffect(() => {
    const m = new GlMap({
      container: el.current!,
      style: STYLE_URL,
      center: lngLat(position),
      zoom: HOME,
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
    })
    m.touchZoomRotate.disableRotation()
    m.on('resize', () => refit.current?.())
    m.once('load', () => {
      // Start the credits collapsed to the small "i" button; tapping it shows them.
      el.current?.querySelector('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show')
      applyTeslaLook(m)
      const firstLabel = m.getStyle().layers.find((l) => l.type === 'symbol')?.id
      m.addSource('satellite', {
        type: 'raster',
        tiles: [IMAGERY],
        tileSize: 256,
        maxzoom: 19,
        attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
      })
      // Under the labels, so satellite keeps its street names.
      m.addLayer({ id: 'satellite', type: 'raster', source: 'satellite', layout: { visibility: 'none' } }, firstLabel)
      m.addSource('radius', { type: 'geojson', data: EMPTY })
      m.addLayer({ id: 'radius', type: 'line', source: 'radius', paint: { 'line-width': 2 } })
      m.addSource('route', { type: 'geojson', data: EMPTY })
      const round = { 'line-cap': 'round', 'line-join': 'round' } as const
      m.addLayer({ id: 'route-casing', type: 'line', source: 'route', layout: round, paint: { 'line-color': '#ffffff', 'line-width': 11 } })
      m.addLayer({ id: 'route', type: 'line', source: 'route', layout: round, paint: { 'line-color': ROUTE_BLUE, 'line-width': 7 } })
      setMap(m)
    })
    return () => {
      m.remove()
      setMap(null)
    }
    // Created once; later position changes are handled by the effects below.
  }, [])

  // Street map or satellite
  useEffect(() => {
    if (!map) return
    map.setLayoutProperty('satellite', 'visibility', satellite ? 'visible' : 'none')
    map.setPaintProperty('radius', 'line-color', satellite ? '#ffffff' : ROUTE_BLUE)
    map.setPaintProperty('radius', 'line-opacity', satellite ? 0.85 : 0.45)
    for (const layer of map.getStyle().layers) {
      if (layer.type !== 'symbol') continue
      map.setPaintProperty(layer.id, 'text-color', satellite ? '#ffffff' : STREET_LABEL)
      map.setPaintProperty(layer.id, 'text-halo-color', satellite ? 'rgba(0,0,0,0.75)' : '#ffffff')
    }
  }, [map, satellite])

  // Chair marker
  useEffect(() => {
    if (!map) return
    const node = document.createElement('div')
    node.className = 'chair-marker'
    node.innerHTML = CHAIR_SVG
    const marker = new Marker({ element: node, rotationAlignment: 'map' }).setLngLat([0, 0]).addTo(map)
    chair.current = marker
    return () => {
      marker.remove()
      chair.current = null
    }
  }, [map])

  useEffect(() => {
    chair.current?.setLngLat(lngLat(position)).setRotation(headingDeg)
  }, [map, position, headingDeg])

  // Search area, and framing while choosing a destination: close in on the chair,
  // widening only as far as needed to show search results.
  useEffect(() => {
    if (!map || mode !== 'idle') return
    const ring = Array.from({ length: 73 }, (_, i) => offset(position, i * 5, SEARCH_RADIUS_M))
    const radius = map.getSource('radius') as GeoJSONSource
    radius.setData(line(ring))
    refit.current = () => {
      if (!results.length) return map.jumpTo({ center: lngLat(position), zoom: HOME })
      map.fitBounds(boundsOf([position, ...results.map((p) => p.location)]), {
        padding: { top: 70, bottom: 70, left: 70, right: 140 },
        maxZoom: HOME,
        animate: false,
      })
    }
    refit.current()
    return () => {
      radius.setData(EMPTY)
      refit.current = null
    }
  }, [map, mode, position.lat, position.lng, results])

  // Search result pins
  useEffect(() => {
    if (!map || mode !== 'idle') return
    const pins = results.map((p) =>
      new Marker({ element: placePin(p.name, () => onSelect(p)), anchor: 'left', offset: [-10, 0] })
        .setLngLat(lngLat(p.location))
        .addTo(map),
    )
    return () => pins.forEach((pin) => pin.remove())
  }, [map, mode, results, onSelect])

  // Route + destination
  useEffect(() => {
    if (!map || !route || !destination) return
    const source = map.getSource('route') as GeoJSONSource
    source.setData(line(route.points))
    const pin = new Marker({ element: placePin(destination.name), anchor: 'left', offset: [-10, 0] })
      .setLngLat(lngLat(destination.location))
      .addTo(map)
    return () => {
      source.setData(EMPTY)
      pin.remove()
    }
  }, [map, route, destination])

  // Whole route before setting off and on arrival
  useEffect(() => {
    if (!map || !route || mode === 'driving' || mode === 'idle') return
    refit.current = () =>
      map.fitBounds(boundsOf(route.points), {
        padding: { top: 170, bottom: 190, left: 60, right: 60 },
        animate: false,
      })
    refit.current()
    return () => {
      refit.current = null
    }
  }, [map, route, mode])

  // Indoor trips have no route to frame; stay on the chair
  useEffect(() => {
    if (!map || route || mode === 'idle') return
    refit.current = () => map.jumpTo({ center: lngLat(position), zoom: HOME })
    refit.current()
    return () => {
      refit.current = null
    }
  }, [map, route, mode, position.lat, position.lng])

  // Follow the chair closely while driving
  useEffect(() => {
    if (!map || mode !== 'driving') return
    map.jumpTo({ center: lngLat(position), zoom: Math.max(map.getZoom(), FOLLOW) })
  }, [map, mode, position])

  return <div ref={el} className="map-canvas" />
}
