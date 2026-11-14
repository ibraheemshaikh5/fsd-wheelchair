import type { LatLng, ManeuverKind, Route, RouteStep } from '../types'
import { angleDelta, bearingDeg, distanceM } from './geo'

const cumCache = new WeakMap<Route, number[]>()

function cumulative(route: Route): number[] {
  let cum = cumCache.get(route)
  if (!cum) {
    cum = [0]
    for (let i = 1; i < route.points.length; i++) {
      cum.push(cum[i - 1] + distanceM(route.points[i - 1], route.points[i]))
    }
    cumCache.set(route, cum)
  }
  return cum
}

export function polylineLength(points: LatLng[]): number {
  let d = 0
  for (let i = 1; i < points.length; i++) d += distanceM(points[i - 1], points[i])
  return d
}

/** Position and travel direction `s` metres along the route. */
export function pointAt(route: Route, s: number): { position: LatLng; headingDeg: number } {
  const pts = route.points
  const cum = cumulative(route)
  const clamped = Math.max(0, Math.min(s, route.lengthM))
  let i = 1
  while (i < pts.length - 1 && cum[i] < clamped) i++
  const a = pts[i - 1]
  const b = pts[i]
  const seg = cum[i] - cum[i - 1]
  const t = seg > 0 ? (clamped - cum[i - 1]) / seg : 0
  return {
    position: { lat: a.lat + (b.lat - a.lat) * t, lng: a.lng + (b.lng - a.lng) * t },
    headingDeg: bearingDeg(a, b),
  }
}

function turnKind(delta: number): ManeuverKind | null {
  const abs = Math.abs(delta)
  if (abs < 25) return null
  const side = delta < 0 ? 'left' : 'right'
  if (abs < 55) return `slight-${side}`
  if (abs < 155) return side
  return 'u-turn'
}

/** Turn-by-turn steps derived purely from the polyline's shape. */
export function stepsFromGeometry(points: LatLng[], destinationName: string): RouteStep[] {
  const steps: RouteStep[] = []
  let offsetM = 0
  for (let i = 1; i < points.length - 1; i++) {
    offsetM += distanceM(points[i - 1], points[i])
    const kind = turnKind(
      angleDelta(bearingDeg(points[i - 1], points[i]), bearingDeg(points[i], points[i + 1])),
    )
    if (kind) steps.push({ kind, label: kind === 'u-turn' ? 'Turn around' : `Turn ${kind.replace('slight-', 'slightly ')}`, offsetM })
  }
  steps.push({ kind: 'arrive', label: destinationName, offsetM: polylineLength(points) })
  return steps
}

export function upcomingSteps(route: Route, progressM: number) {
  const ahead = route.steps
    .filter((s) => s.offsetM > progressM + 0.5 || s.kind === 'arrive')
    .map((step) => ({ step, distanceM: Math.max(0, step.offsetM - progressM) }))
  return { next: ahead[0] ?? null, following: ahead[1] ?? null }
}
