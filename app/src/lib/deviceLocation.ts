import type { LatLng } from '../types'

// The iPad's own location, shared with whoever needs it. In the real system the
// chair's position comes from the driving service, not from here.

/** Used until a real location is known, and whenever access is refused. */
export const DEFAULT_POSITION: LatLng = { lat: 42.3868, lng: -71.1768 }

type Listener = (p: LatLng) => void
const listeners = new Set<Listener>()
let last: LatLng | null = null

/** Calls `fn` with the iPad's location whenever one is obtained. */
export function onDeviceLocation(fn: Listener): () => void {
  listeners.add(fn)
  if (last) fn(last)
  return () => listeners.delete(fn)
}

export function publishDeviceLocation(p: LatLng) {
  last = p
  listeners.forEach((fn) => fn(p))
}
