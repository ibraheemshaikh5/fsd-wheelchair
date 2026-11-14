import type { LatLng, ManeuverKind, Place, Route, RouteStep } from '../types'
import { ensureGoogle } from '../lib/googleMaps'
import { distanceM } from '../lib/geo'
import { polylineLength, stepsFromGeometry } from '../lib/route'

/**
 * Stand-in route planner used by the mock driving service. The real service plans
 * its own route and sends it in DriveState, so nothing in the UI calls this.
 */
export async function planRoute(origin: LatLng, destination: Place): Promise<Route> {
  if (await ensureGoogle()) {
    try {
      return await googleWalkingRoute(origin, destination)
    } catch {
      // fall through to the offline route
    }
  }
  // One corner, so the demo has a turn in it.
  const points = [origin, { lat: origin.lat, lng: destination.location.lng }, destination.location]
  return { points, steps: stepsFromGeometry(points, destination.name), lengthM: polylineLength(points) }
}

function maneuverKind(maneuver: string): ManeuverKind {
  const m = maneuver.toLowerCase()
  if (m.includes('uturn')) return 'u-turn'
  const side = m.includes('left') ? 'left' : m.includes('right') ? 'right' : null
  if (!side) return 'straight'
  return m.includes('slight') || m.includes('keep') ? `slight-${side}` : side
}

/** "Turn left onto Trapelo Rd" -> "Trapelo Rd". Falls back to the whole instruction. */
function stepLabel(instruction: string): string {
  const text = instruction
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/\s*Destination will be.*$/i, '')
    .trim()
  return text.match(/\b(?:onto|on|toward|towards)\s+(.+)$/i)?.[1] ?? text
}

async function googleWalkingRoute(origin: LatLng, destination: Place): Promise<Route> {
  const { routes } = await google.maps.routes.Route.computeRoutes({
    origin,
    destination: destination.location,
    travelMode: 'WALKING',
    fields: ['path', 'legs'],
  })
  const leg = routes?.[0]?.legs?.[0]
  if (!leg) throw new Error('No walking route found')
  const points: LatLng[] = []
  const steps: RouteStep[] = []
  let offsetM = 0
  leg.steps.forEach((s, i) => {
    if (i > 0) steps.push({ kind: maneuverKind(s.maneuver ?? ''), label: stepLabel(s.instructions ?? ''), offsetM })
    for (const p of s.path) {
      // Consecutive steps repeat their shared point; a zero-length segment has no direction.
      const last = points.at(-1)
      if (!last || distanceM(last, p) > 0.3) points.push({ lat: p.lat, lng: p.lng })
    }
    offsetM += s.distanceMeters
  })
  if (points.length < 2) throw new Error('Walking route has no path')
  const lengthM = polylineLength(points)
  // Step offsets come from Google's rounded distances; keep them on the polyline.
  for (const s of steps) s.offsetM = Math.min(s.offsetM, lengthM)
  steps.push({ kind: 'arrive', label: destination.name, offsetM: lengthM })
  return { points, steps, lengthM }
}
