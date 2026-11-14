import type { LatLng, LocalPoint } from '../types'

const R = 6371000
const rad = (d: number) => (d * Math.PI) / 180
const deg = (r: number) => (r * 180) / Math.PI

export function distanceM(a: LatLng, b: LatLng): number {
  const dLat = rad(b.lat - a.lat)
  const dLng = rad(b.lng - a.lng)
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

export function bearingDeg(a: LatLng, b: LatLng): number {
  const y = Math.sin(rad(b.lng - a.lng)) * Math.cos(rad(b.lat))
  const x =
    Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) -
    Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lng - a.lng))
  return (deg(Math.atan2(y, x)) + 360) % 360
}

/** Point `dist` metres from `p` along `bearing`. Flat-earth; fine at walking range. */
export function offset(p: LatLng, bearing: number, dist: number): LatLng {
  const north = Math.cos(rad(bearing)) * dist
  const east = Math.sin(rad(bearing)) * dist
  return {
    lat: p.lat + deg(north / R),
    lng: p.lng + deg(east / (R * Math.cos(rad(p.lat)))),
  }
}

/** East/north metres of `p` relative to `origin`. */
export function toEastNorth(origin: LatLng, p: LatLng): { e: number; n: number } {
  return {
    e: rad(p.lng - origin.lng) * R * Math.cos(rad(origin.lat)),
    n: rad(p.lat - origin.lat) * R,
  }
}

export function toLocal(origin: LatLng, headingDeg: number, p: LatLng): LocalPoint {
  const { e, n } = toEastNorth(origin, p)
  const h = rad(headingDeg)
  return { x: e * Math.cos(h) - n * Math.sin(h), z: e * Math.sin(h) + n * Math.cos(h) }
}

export function boundsAround(p: LatLng, radiusM: number) {
  const ne = offset(offset(p, 0, radiusM), 90, radiusM)
  const sw = offset(offset(p, 180, radiusM), 270, radiusM)
  return { north: ne.lat, east: ne.lng, south: sw.lat, west: sw.lng }
}

/** Signed smallest difference b - a, in (-180, 180]. */
export function angleDelta(a: number, b: number): number {
  return ((b - a + 540) % 360) - 180
}
