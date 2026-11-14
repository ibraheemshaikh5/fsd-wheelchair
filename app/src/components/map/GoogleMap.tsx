import { useEffect, useRef, useState } from 'react'
import { loadGoogleMaps } from '../../lib/googleMaps'
import { SEARCH_RADIUS_M } from '../../services/places'
import { CHAIR_RED, FOLLOW_ZOOM, HOME_ZOOM, ROUTE_BLUE, type MapProps } from './shared'

const LABEL_TEXT = '#5f6268'

// The in-car look: light grey land, white roads, darker grey buildings, no colour.
const TESLA_STYLE: google.maps.MapTypeStyle[] = [
  { featureType: 'landscape.natural', elementType: 'geometry', stylers: [{ color: '#ececed' }] },
  // This feature type covers both the ground of a built-up block and the buildings
  // on it. A fixed colour would paint them the same and hide the buildings, so shift
  // the tones instead and outline each footprint.
  {
    featureType: 'landscape.man_made',
    elementType: 'geometry.fill',
    stylers: [{ saturation: -100 }, { lightness: -3 }, { gamma: 0.6 }],
  },
  { featureType: 'landscape.man_made', elementType: 'geometry.stroke', stylers: [{ color: '#c9c9cd' }, { weight: 1 }] },
  { featureType: 'poi', elementType: 'geometry', stylers: [{ color: '#e2e2e2' }] },
  { featureType: 'poi.park', elementType: 'geometry', stylers: [{ color: '#dcdddb' }] },
  { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#ffffff' }] },
  { featureType: 'road', elementType: 'geometry.stroke', stylers: [{ visibility: 'off' }] },
  { featureType: 'road', elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
  { featureType: 'transit', stylers: [{ visibility: 'off' }] },
  { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#d0d0d0' }] },
  { elementType: 'labels.icon', stylers: [{ saturation: -100 }, { lightness: 20 }] },
  // Labels dark enough to read in sunlight (about 5:1 on the grey land).
  { elementType: 'labels.text.fill', stylers: [{ color: LABEL_TEXT }] },
  // A halo the colour of the land rather than white, so the letters read thin.
  { elementType: 'labels.text.stroke', stylers: [{ color: '#ececed' }, { weight: 2 }] },
  { featureType: 'road', elementType: 'labels.text.fill', stylers: [{ color: LABEL_TEXT }] },
  { featureType: 'road', elementType: 'labels.text.stroke', stylers: [{ color: '#ffffff' }, { weight: 1 }] },
]

/** Blue dot with a white ring, matching the keyless map's pins. */
function dot(scale: number): google.maps.Symbol {
  return {
    path: google.maps.SymbolPath.CIRCLE,
    scale,
    fillColor: ROUTE_BLUE,
    fillOpacity: 1,
    strokeColor: '#ffffff',
    strokeWeight: 3,
  }
}

export function GoogleMap({ state, results, onSelect, satellite }: MapProps) {
  const el = useRef<HTMLDivElement>(null)
  const [map, setMap] = useState<google.maps.Map | null>(null)
  const chair = useRef<google.maps.Marker | null>(null)
  const { mode, position, headingDeg, route, destination } = state

  useEffect(() => {
    let cancelled = false
    loadGoogleMaps()
      .then(() => {
        if (cancelled || !el.current) return
        setMap(
          new google.maps.Map(el.current, {
            center: position,
            zoom: HOME_ZOOM,
            // Whole zoom levels only: in-between levels stretch the label text and blur it.
            isFractionalZoomEnabled: false,
            disableDefaultUI: true,
            clickableIcons: false,
            gestureHandling: 'greedy',
            styles: TESLA_STYLE,
            tilt: 0,
          }),
        )
      })
      .catch(() => {}) // MapPane swaps in the keyless map on failure
    return () => {
      cancelled = true
    }
    // Created once; later position changes are handled by the effects below.
  }, [])

  useEffect(() => {
    map?.setMapTypeId(satellite ? 'hybrid' : 'roadmap')
  }, [map, satellite])

  // Chair marker. The legacy Marker is used throughout because its replacement
  // needs a cloud map ID, which would switch off the JSON styling above.
  useEffect(() => {
    if (!map) return
    chair.current ??= new google.maps.Marker({ map, zIndex: 10 })
    chair.current.setPosition(position)
    chair.current.setIcon({
      path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW,
      scale: 6.5,
      rotation: headingDeg,
      fillColor: CHAIR_RED,
      fillOpacity: 1,
      strokeColor: '#ffffff',
      strokeWeight: 2,
    })
  }, [map, position, headingDeg])

  // Search area, and framing while choosing a destination: close in on the chair,
  // widening only as far as needed to show search results.
  useEffect(() => {
    if (!map || mode !== 'idle') return
    const circle = new google.maps.Circle({
      map,
      center: position,
      radius: SEARCH_RADIUS_M,
      strokeColor: satellite ? '#ffffff' : ROUTE_BLUE,
      strokeOpacity: satellite ? 0.85 : 0.45,
      strokeWeight: 2,
      fillOpacity: 0,
      clickable: false,
    })
    let clamp: google.maps.MapsEventListener | null = null
    if (results.length) {
      const bounds = new google.maps.LatLngBounds(position)
      results.forEach((p) => bounds.extend(p.location))
      map.fitBounds(bounds, 70)
      // fitBounds has no zoom cap, so one nearby result would zoom right in.
      // Cap it once the framing settles, like the keyless map's maxZoom.
      clamp = google.maps.event.addListenerOnce(map, 'idle', () => {
        if ((map.getZoom() ?? 0) > HOME_ZOOM) map.setZoom(HOME_ZOOM)
      })
    } else {
      map.setCenter(position)
      map.setZoom(HOME_ZOOM)
    }
    return () => {
      // A re-run before the map settles must not clamp the next framing.
      clamp?.remove()
      circle.setMap(null)
    }
  }, [map, mode, position.lat, position.lng, results, satellite])

  // Search result pins
  useEffect(() => {
    if (!map || mode !== 'idle') return
    const markers = results.map((p) => {
      const m = new google.maps.Marker({ map, position: p.location, title: p.name, icon: dot(9) })
      m.addListener('click', () => onSelect(p))
      return m
    })
    return () => markers.forEach((m) => m.setMap(null))
  }, [map, mode, results, onSelect])

  // Route + destination
  useEffect(() => {
    if (!map || !route || !destination) return
    const line = new google.maps.Polyline({
      map,
      path: route.points,
      strokeColor: ROUTE_BLUE,
      strokeOpacity: 1,
      strokeWeight: 7,
    })
    const pin = new google.maps.Marker({ map, position: destination.location, title: destination.name, icon: dot(10) })
    const bounds = new google.maps.LatLngBounds()
    route.points.forEach((p) => bounds.extend(p))
    map.fitBounds(bounds, { top: 190, bottom: 170, left: 60, right: 60 })
    return () => {
      line.setMap(null)
      pin.setMap(null)
    }
  }, [map, route, destination])

  // Indoor trips have no route to frame; stay on the chair
  useEffect(() => {
    if (!map || route || mode === 'idle') return
    map.setCenter(position)
    map.setZoom(HOME_ZOOM)
  }, [map, route, mode, position.lat, position.lng])

  // Follow the chair while driving
  useEffect(() => {
    if (!map || mode !== 'driving') return
    if ((map.getZoom() ?? 0) < FOLLOW_ZOOM) map.setZoom(FOLLOW_ZOOM)
    map.panTo(position)
  }, [map, mode, position])

  return <div ref={el} className="map-canvas" />
}
