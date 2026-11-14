import type { DriveState, LocalPoint, Obstacle, PathStatus, Place, Route } from '../types'
import { onDeviceLocation } from '../lib/deviceLocation'
import { distanceLabel } from '../lib/format'
import { angleDelta, toLocal } from '../lib/geo'
import { pointAt, upcomingSteps } from '../lib/route'
import type { DrivingService } from './drivingService'
import { planRoute } from './routing'

const TICK_MS = 100
const CRUISE_MPS = 1.25
const INDOOR_MPS = 0.8
const MAX_MPS = 1.8 // about 4 mph
const ACCEL_MPS2 = 0.6
const LOOKAHEAD_M = 30
const INDOOR_DEMO_M = 25 // the pretend indoor trip ends after this distance
// Used until the iPad's location is known, and whenever access is refused.
const DEFAULT_POSITION = { lat: 42.3868, lng: -71.1768 }

// How close to the chair's line of travel, and how far ahead, something has to be
// before the chair reacts to it.
const CORRIDOR_HALF_WIDTH_M = 0.75
const SLOW_WITHIN_M = 6
const STOP_WITHIN_M = 2.4

/** A simulated person, with their own motion in the chair's frame. */
interface Walker extends Obstacle {
  vx: number
  vz: number
}

/**
 * Simulates the driving service in the browser so the UI can be built without it.
 * In development, press "t" while driving to simulate the chair asking the rider
 * to take over.
 */
export class MockDrivingService implements DrivingService {
  private state: DriveState = {
    mode: 'idle',
    connected: true,
    position: DEFAULT_POSITION,
    headingDeg: 0,
    speedMps: 0,
    maxSpeedMps: MAX_MPS,
    batteryPct: 79,
    destination: null,
    route: null,
    languageGoal: null,
    progressM: 0,
    remainingM: 0,
    etaSeconds: 0,
    nextStep: null,
    followingStep: null,
    path: [],
    pathStatus: 'clear',
    obstacles: [],
    yieldingToId: null,
    reason: null,
    takeOver: null,
  }
  private walkers: Walker[] = []
  private indoorM = 0
  private listeners = new Set<() => void>()
  private timer: ReturnType<typeof setInterval> | null = null
  private nextWalkerId = 0
  private planning = 0
  private camera: MediaStream | null = null

  constructor() {
    // The real chair position comes from the phone mounted on it. Here the
    // iPad's own location stands in, so searches are centred somewhere real.
    onDeviceLocation((position) => {
      if (this.state.mode === 'idle') this.set({ position })
    })
    if (import.meta.env.DEV) {
      window.addEventListener('keydown', (e) => {
        const typing = e.target instanceof HTMLInputElement
        if (e.key === 't' && !typing && this.state.mode === 'driving') this.requestTakeOver('Path blocked ahead')
      })
    }
  }

  getState = () => this.state

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async setDestination(place: Place) {
    const request = ++this.planning
    this.stopLoop()
    const route = await planRoute(this.state.position, place)
    if (request !== this.planning) return
    const start = pointAt(route, 0)
    this.walkers = []
    this.set({
      ...TRIP_RESET,
      mode: 'ready',
      destination: place,
      route,
      // Routes begin on the nearest path, which can be a few metres from the GPS fix.
      position: start.position,
      headingDeg: start.headingDeg,
      reason: 'Ready when you are',
      ...routeView(route, 0, start.position, start.headingDeg),
    })
  }

  async setLanguageGoal(instruction: string) {
    this.planning++
    this.stopLoop()
    this.indoorM = 0
    this.walkers = []
    this.set({
      ...TRIP_RESET,
      mode: 'ready',
      languageGoal: instruction,
      path: indoorPath(0),
      reason: 'Ready when you are',
    })
  }

  clearDestination() {
    this.planning++
    this.stopLoop()
    this.walkers = []
    this.set({ ...TRIP_RESET, mode: 'idle' })
  }

  engage() {
    if (this.state.mode !== 'ready' && this.state.mode !== 'manual') return
    if (!this.state.route && !this.state.languageGoal) return
    this.set({ mode: 'driving', takeOver: null, reason: 'Setting off' })
    this.timer ??= setInterval(() => this.tick(TICK_MS / 1000), TICK_MS)
  }

  disengage() {
    if (this.state.mode !== 'driving') return
    this.stopLoop()
    this.set({ mode: 'ready', speedMps: 0, pathStatus: 'clear', yieldingToId: null, reason: 'Holding' })
  }

  async openCamera() {
    // The real feed comes from the phone on the chair. The iPad's own camera stands in.
    if (this.camera) return this.camera
    try {
      this.camera = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
    } catch {
      this.camera = null
    }
    return this.camera
  }

  closeCamera() {
    this.camera?.getTracks().forEach((t) => t.stop())
    this.camera = null
  }

  /** Stops the chair and hands control to the rider. */
  private requestTakeOver(why: string) {
    this.stopLoop()
    this.set({ mode: 'manual', speedMps: 0, takeOver: why, pathStatus: 'blocked', reason: 'Waiting for you' })
  }

  private stopLoop() {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  private set(patch: Partial<DriveState>) {
    this.state = { ...this.state, ...patch }
    this.listeners.forEach((fn) => fn())
  }

  private tick(dt: number) {
    const { route } = this.state
    const total = route ? route.lengthM : INDOOR_DEMO_M
    const travelled = route ? this.state.progressM : this.indoorM
    const remaining = total - travelled

    if (remaining <= 0.05) {
      this.stopLoop()
      this.walkers = []
      this.set({
        mode: 'arrived',
        speedMps: 0,
        remainingM: 0,
        etaSeconds: 0,
        path: [],
        obstacles: [],
        pathStatus: 'clear',
        yieldingToId: null,
        reason: 'Arrived',
      })
      return
    }

    // What is in the way decides how fast the chair may go.
    const inWay = this.walkers
      .filter((w) => Math.abs(w.x) < CORRIDOR_HALF_WIDTH_M && w.z > 0.4 && w.z < SLOW_WITHIN_M)
      .sort((a, b) => a.z - b.z)[0]
    const pathStatus: PathStatus = !inWay ? 'clear' : inWay.z < STOP_WITHIN_M ? 'blocked' : 'slowing'
    const cruise = route ? CRUISE_MPS : INDOOR_MPS
    const limit = pathStatus === 'blocked' ? 0 : pathStatus === 'slowing' ? cruise * 0.45 : cruise
    // Slow down so the chair can stop at the destination.
    const target = Math.min(limit, Math.sqrt(2 * ACCEL_MPS2 * remaining) + 0.1)
    const brake = pathStatus === 'blocked' ? ACCEL_MPS2 * 2.5 : ACCEL_MPS2
    const speedMps =
      this.state.speedMps + Math.max(-brake * dt, Math.min(ACCEL_MPS2 * dt, target - this.state.speedMps))

    this.stepWalkers(dt, speedMps)
    const shared = {
      speedMps,
      pathStatus,
      yieldingToId: inWay?.id ?? null,
      obstacles: this.walkers.map(({ id, kind, x, z }) => ({ id, kind, x, z })),
    }

    if (!route) {
      this.indoorM += speedMps * dt
      this.set({ ...shared, path: indoorPath(this.indoorM), reason: reasonFor(pathStatus, remaining, null, false) })
      return
    }
    const progressM = Math.min(route.lengthM, this.state.progressM + speedMps * dt)
    const at = pointAt(route, progressM)
    const headingDeg =
      (this.state.headingDeg + angleDelta(this.state.headingDeg, at.headingDeg) * Math.min(1, dt * 2.5) + 360) % 360
    const view = routeView(route, progressM, at.position, headingDeg)
    this.set({
      ...shared,
      ...view,
      progressM,
      position: at.position,
      headingDeg,
      reason: reasonFor(pathStatus, remaining, view.nextStep, true),
    })
  }

  private stepWalkers(dt: number, chairSpeed: number) {
    this.walkers = this.walkers
      .map((w) => ({ ...w, x: w.x + w.vx * dt, z: w.z + (w.vz - chairSpeed) * dt }))
      .filter((w) => w.z > -6 && w.z < 40 && Math.abs(w.x) < 6)
    if (this.walkers.length >= 4 || Math.random() > dt * 0.3) return
    const side = Math.random() < 0.5 ? -1 : 1
    const id = `ped-${this.nextWalkerId++}`
    if (Math.random() < 0.3) {
      // Someone crossing in front of the chair.
      this.walkers.push({ id, kind: 'pedestrian', x: side * 4.5, z: 6 + Math.random() * 3, vx: -side * 0.8, vz: 0 })
    } else {
      // Someone walking towards the chair, off to one side.
      this.walkers.push({ id, kind: 'pedestrian', x: side * (0.95 + Math.random() * 1.8), z: 32, vx: 0, vz: -0.9 })
    }
  }
}

/** One line on what the chair is doing, most pressing reason first. */
function reasonFor(status: PathStatus, remainingM: number, next: DriveState['nextStep'], onRoute: boolean): string {
  if (status === 'blocked') return 'Waiting for pedestrian'
  if (status === 'slowing') return 'Slowing for pedestrian'
  if (remainingM < 8) return 'Arriving'
  if (next && next.step.kind !== 'arrive' && next.step.kind !== 'straight' && next.distanceM < 20) {
    const turn = next.step.kind === 'u-turn' ? 'Turning around' : `Turning ${next.step.kind.replace('slight-', '')}`
    return `${turn} in ${distanceLabel(next.distanceM)}`
  }
  return onRoute ? 'Following the route' : 'Heading to your goal'
}

/** Everything the UI shows that follows from where the chair is along its route. */
function routeView(route: Route, progressM: number, position: DriveState['position'], headingDeg: number) {
  const remainingM = route.lengthM - progressM
  const path: LocalPoint[] = []
  for (let d = 0; d <= Math.min(LOOKAHEAD_M, remainingM); d += 1.5) {
    path.push(toLocal(position, headingDeg, pointAt(route, progressM + d).position))
  }
  const { next, following } = upcomingSteps(route, progressM)
  return { remainingM, etaSeconds: remainingM / CRUISE_MPS, path, nextStep: next, followingStep: following }
}

/** Everything that describes a trip, cleared. */
const TRIP_RESET = {
  destination: null,
  route: null,
  languageGoal: null,
  progressM: 0,
  remainingM: 0,
  etaSeconds: 0,
  speedMps: 0,
  nextStep: null,
  followingStep: null,
  path: [],
  pathStatus: 'clear',
  obstacles: [],
  yieldingToId: null,
  reason: null,
  takeOver: null,
} satisfies Partial<DriveState>

/** A gently weaving path ahead of the chair, `travelled` metres into the trip. */
function indoorPath(travelled: number): LocalPoint[] {
  const path: LocalPoint[] = []
  for (let z = 0; z <= 30; z += 1.5) {
    path.push({ x: 0.9 * (Math.sin(0.3 * (z + travelled)) - Math.sin(0.3 * travelled)), z })
  }
  return path
}
