import type { LatLng } from '../types'

// The iPad's own location, shared with whoever needs it. In the real system the
// chair's position comes from the driving service, not from here.

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
