import type { DriveState, LatLng, Place, Route } from '../../types'
import { DEFAULT_POSITION, onDeviceLocation } from '../../lib/deviceLocation'
import type { CameraFeed, CameraFrame, DrivingService } from '../drivingService'
import { ServiceRefusedError } from '../errors'
import { planRoute } from '../routing'
import { deriveState, type Trip } from './liveState'
import { sameValue } from './sameValue'
import type { ServiceStatus } from './status'
import {
  connectViewer,
  type SocketState,
  type Timers,
  type ViewerConnection,
  type ViewerSocket,
  viewerSocketUrl,
} from './viewerSocket'

const DESTINATION_URL = '/api/destination'
const GOAL_URL = '/api/goal'
/** The service takes at most this many route points and this long an instruction. */
const MAX_ROUTE_POINTS = 2000
const MAX_INSTRUCTION_CHARS = 200
/**
 * For showing the picture (not for driving), it is live while the iPad itself
 * received a frame this recently. The service's frame.fresh is the strict
 * drive-safety check (500 ms from capture, needs clock sync), which a short
 * network hiccup or a clock re-sync trips; that must not blank the camera view.
 */
export const DISPLAY_FRAME_FRESH_MS = 1000
/**
 * Frames that keep arriving but were captured longer ago than this (a backlog or
 * a slow link) are shown as delayed, so the rider knows the picture is behind.
 * Known only once the phone clock is synced; until then no delay is claimed.
 */
export const DISPLAY_FRAME_MAX_AGE_MS = 1000
/**
 * Frame URLs kept valid at once. At 30 fps React may skip rendering some frames,
 * so an image can still be loading a URL a frame or two after it was handed out;
 * revoking only older ones keeps it from breaking mid-swap.
 */
export const FRAME_URLS_KEPT = 4

type PictureState = Pick<CameraFrame, 'fresh' | 'delayed'>

export interface LiveOptions {
  socketUrl?: string
  createSocket?: (url: string) => ViewerSocket
  timers?: Timers
  fetchFn?: typeof fetch
  planRoute?: typeof planRoute
  createObjectUrl?: (blob: Blob) => string
  revokeObjectUrl?: (url: string) => void
  /** Subscribes to the iPad's own location; returns an unsubscribe function. */
  onIpadLocation?: (fn: (p: LatLng) => void) => () => void
  /** Milliseconds on a steady clock, for how long ago the last frame arrived. */
  now?: () => number
}

/** Why a request to the service failed, from its {success, data, error} envelope or the HTTP status. */
async function requestFailure(response: Response): Promise<string | null> {
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // not JSON: fall back to the HTTP status below
  }
  const envelope = typeof body === 'object' && body !== null ? (body as { success?: unknown; error?: unknown }) : {}
  if (response.ok && envelope.success === true) return null
  return typeof envelope.error === 'string' ? envelope.error : `HTTP ${response.status}`
}

/**
 * A route's points in the service's shape. A longer route than the service takes
 * is thinned evenly, always keeping its last point: the destination.
 */
export function routePoints(route: Route): { lat: number; lon: number }[] {
  const all = route.points
  const step = Math.max(1, Math.ceil(all.length / MAX_ROUTE_POINTS))
  const kept = all.filter((_, index) => index % step === 0 || index === all.length - 1)
  return kept.slice(-MAX_ROUTE_POINTS).map((point) => ({ lat: point.lat, lon: point.lng }))
}

/**
 * The real driving service, over its /ws/viewer socket and /api/destination.
 *
 * The service streams the phone's camera and sensors and keeps the trip: the
 * destination, plus the route or indoor instruction a driving model follows;
 * it does not drive yet, so engage() is never offered (DriveState.engageBlocked
 * says why) and the rider drives with the joystick while the iPad follows along.
 */
export class LiveDrivingService implements DrivingService {
  private socket: SocketState = 'connecting'
  private status: ServiceStatus | null = null
  private ipadPosition: LatLng | null = null
  private trip: Trip | null = null
  private planning = 0
  /** Set on each new connection until the first status has been reconciled. */
  private needsSync = false
  /** True from losing the service until it sends a usable status again. */
  private linkLost = false
  private linkIssue: string | null = null
  /** When this iPad last received a frame it counts as live on the current connection (`now()` ms). */
  private lastFrameAtMs: number | null = null
  /** Frames received from a connected phone on the current connection. */
  private framesThisConnection = 0
  /** What the camera view was last told about the picture, so changes are pushed once. */
  private cameraWas: PictureState | null = null
  private state: DriveState
  private readonly listeners = new Set<() => void>()
  private readonly cameraListeners = new Set<(frame: CameraFrame) => void>()
  /** The newest frame URLs, oldest first: the one on screen and those replacing it. */
  private frameUrls: string[] = []
  private readonly connection: ViewerConnection
  private readonly stopIpadLocation: () => void
  private readonly fetchFn: typeof fetch
  private readonly plan: typeof planRoute
  private readonly createObjectUrl: (blob: Blob) => string
  private readonly revokeObjectUrl: (url: string) => void
  private readonly now: () => number

  constructor(options: LiveOptions = {}) {
    this.fetchFn = options.fetchFn ?? ((input, init) => fetch(input, init))
    this.plan = options.planRoute ?? planRoute
    this.createObjectUrl = options.createObjectUrl ?? ((blob) => URL.createObjectURL(blob))
    this.revokeObjectUrl = options.revokeObjectUrl ?? ((url) => URL.revokeObjectURL(url))
    this.now = options.now ?? (() => performance.now())
    this.state = this.derive({ progressM: 0, headingDeg: 0, arrived: false })
    this.stopIpadLocation = (options.onIpadLocation ?? onDeviceLocation)((position) => {
      this.ipadPosition = position
      this.update()
    })
    this.connection = connectViewer({
      url: options.socketUrl ?? viewerSocketUrl(window.location),
      createSocket: options.createSocket ?? ((url) => new WebSocket(url)),
      timers: options.timers ?? {
        set: (fn, ms) => window.setTimeout(fn, ms),
        clear: (id) => window.clearTimeout(id),
      },
      onState: (socket, reason) => this.onSocketState(socket, reason),
      onStatus: (status) => {
        this.status = status
        this.linkLost = false
        this.linkIssue = null
        this.update()
        this.notifyCamera()
        this.syncDestination(status)
      },
      onFrame: (jpeg) => this.onFrame(jpeg),
    })
  }

  getState = () => this.state

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  async setDestination(place: Place): Promise<void> {
    const request = ++this.planning
    const route = await this.plan(this.state.position, place)
    if (request !== this.planning) return
    const failure = await this.sendDestination(place)
    if (request !== this.planning) return
    if (failure) throw new ServiceRefusedError(`Couldn’t send ${place.name} to the chair: ${failure}`)
    this.trip = { kind: 'place', place, route }
    this.update({ progressM: 0, arrived: false })
    await this.sendRoute(route)
  }

  async setLanguageGoal(instruction: string): Promise<void> {
    this.planning++
    this.trip = { kind: 'indoor', instruction }
    this.update({ progressM: 0, arrived: false })
    await this.sendInstruction(instruction)
  }

  clearDestination(): void {
    this.planning++
    this.trip = null
    this.update({ progressM: 0, arrived: false })
    void this.clearServiceDestination()
  }

  /** Not available until the service has a driving loop; the UI shows engageBlocked instead. */
  engage(): void {}

  /** Nothing to stop: the service never drives the chair yet. */
  disengage(): void {}

  async openCamera(): Promise<CameraFeed> {
    return {
      kind: 'frames',
      subscribe: (listener) => {
        this.cameraListeners.add(listener)
        // Tell a new viewer straight away whether a live picture is coming.
        listener({ url: this.frameUrls.at(-1) ?? null, ...this.picture() })
        return () => {
          this.cameraListeners.delete(listener)
          if (this.cameraListeners.size === 0) this.releaseFrames()
        }
      },
    }
  }

  closeCamera(): void {
    this.cameraListeners.clear()
    this.releaseFrames()
  }

  /** Stops listening to the service and the iPad's location. */
  dispose(): void {
    this.connection.close()
    this.stopIpadLocation()
    this.closeCamera()
  }

  private onSocketState(socket: SocketState, reason: string | null) {
    if (socket === 'online' && this.socket !== 'online') this.needsSync = true
    if (socket === 'offline') {
      this.linkLost = true
      this.linkIssue = reason
    }
    this.socket = socket
    if (socket !== 'online') {
      this.status = null
      this.lastFrameAtMs = null
      this.framesThisConnection = 0
    }
    this.update()
    this.notifyCamera()
  }

  /**
   * Once per connection, make the service's destination match this iPad's trip.
   * The service keeps it in memory only, so after a restart it is gone; and after
   * the iPad reloads, the service may still hold a trip the rider no longer sees.
   */
  private syncDestination(status: ServiceStatus) {
    if (!this.needsSync) return
    this.needsSync = false
    if (this.trip?.kind === 'place') {
      const { place, route } = this.trip
      void this.sendDestination(place).then(async (failure) => {
        if (!failure) {
          await this.sendRoute(route)
          return
        }
        console.warn(`Could not resend the destination to the service: ${failure}`)
        // Try again on the next connection; retrying on every status would flood it.
      })
    } else if (this.trip?.kind === 'indoor') {
      void this.sendInstruction(this.trip.instruction)
    } else if (status.destination) {
      void this.clearServiceDestination()
    }
  }

  /** A live picture: the iPad hears the service and itself received a frame within DISPLAY_FRAME_FRESH_MS. */
  private cameraFresh(): boolean {
    if (this.socket !== 'online' || this.linkLost || this.lastFrameAtMs === null) return false
    return this.now() - this.lastFrameAtMs <= DISPLAY_FRAME_FRESH_MS
  }

  /**
   * The newest frame was already over DISPLAY_FRAME_MAX_AGE_MS old when it reached
   * the iPad (clock synced only). age_ms counts from capture and keeps growing after
   * frames stop, so the time since this iPad received a frame is taken off; otherwise
   * every stall would read "delayed" for a moment before "no live picture".
   */
  private captureDelayed(): boolean {
    const ageMs = this.status?.clock_synced ? (this.status.frame?.age_ms ?? null) : null
    if (ageMs === null || this.lastFrameAtMs === null) return false
    return ageMs - (this.now() - this.lastFrameAtMs) > DISPLAY_FRAME_MAX_AGE_MS
  }

  /**
   * Whether a frame that just arrived shows the phone's camera as it is now. The
   * service keeps the phone's last frame and replays it to every viewer that
   * connects, however old: never live once the phone has gone, and, as the first
   * frame of a connection, only when the service calls it fresh. A second frame
   * proves pictures are flowing, whatever the strict status says.
   */
  private frameIsLive(): boolean {
    if (!this.status?.phone_connected) return false
    this.framesThisConnection += 1
    return this.framesThisConnection > 1 || this.status.frame?.fresh === true
  }

  private picture(): PictureState {
    const fresh = this.cameraFresh()
    return { fresh, delayed: fresh && this.captureDelayed() }
  }

  /**
   * Frames stop arriving when the phone does, so freshness is re-checked on every
   * status (about 30 a second) and changes are pushed without waiting for a frame.
   */
  private notifyCamera() {
    if (this.cameraListeners.size === 0) return
    const picture = this.picture()
    if (this.cameraWas && sameValue(picture, this.cameraWas)) return
    this.cameraWas = picture
    const frame = { url: this.frameUrls.at(-1) ?? null, ...picture }
    this.cameraListeners.forEach((fn) => fn(frame))
  }

  private onFrame(jpeg: ArrayBuffer) {
    if (this.frameIsLive()) this.lastFrameAtMs = this.now()
    // Frames arrive whether or not anyone is looking; only decode them for the camera view.
    if (this.cameraListeners.size === 0) return
    const url = this.createObjectUrl(new Blob([jpeg], { type: 'image/jpeg' }))
    const kept = [...this.frameUrls, url]
    const expired = kept.slice(0, Math.max(0, kept.length - FRAME_URLS_KEPT))
    this.frameUrls = kept.slice(expired.length)
    expired.forEach((old) => this.revokeObjectUrl(old))
    const picture = this.picture()
    this.cameraWas = picture
    const frame = { url, ...picture }
    this.cameraListeners.forEach((fn) => fn(frame))
  }

  private releaseFrames() {
    this.frameUrls.forEach((url) => this.revokeObjectUrl(url))
    this.frameUrls = []
    this.cameraWas = null
  }

  private async sendDestination(place: Place): Promise<string | null> {
    const body = JSON.stringify({ lat: place.location.lat, lon: place.location.lng })
    try {
      const response = await this.fetchFn(DESTINATION_URL, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body,
      })
      return await requestFailure(response)
    } catch (error) {
      return error instanceof Error ? error.message : 'the service could not be reached'
    }
  }

  /**
   * The route is what a driving model follows. The destination is sent first, so
   * a service too old to take a route still knows where the rider is going.
   */
  private async sendRoute(route: Route): Promise<void> {
    const points = routePoints(route)
    if (points.length < 2) return
    const failure = await this.sendGoal({ kind: 'route', points })
    if (failure) console.warn(`Could not send the route to the service: ${failure}`)
  }

  /** Indoors there is no map point: the words themselves are the goal. */
  private async sendInstruction(instruction: string): Promise<void> {
    const text = instruction.trim().slice(0, MAX_INSTRUCTION_CHARS)
    const failure = text ? await this.sendGoal({ kind: 'instruction', text }) : 'nothing was said'
    if (!failure) return
    console.warn(`Could not send the instruction to the service: ${failure}`)
    // At least make sure the chair is not still heading for the last map point.
    await this.clearServiceDestination()
  }

  private async sendGoal(goal: object): Promise<string | null> {
    try {
      const response = await this.fetchFn(GOAL_URL, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(goal),
      })
      return await requestFailure(response)
    } catch (error) {
      return error instanceof Error ? error.message : 'the service could not be reached'
    }
  }

  private async clearServiceDestination(): Promise<void> {
    try {
      const failure = await requestFailure(await this.fetchFn(DESTINATION_URL, { method: 'DELETE' }))
      if (failure) console.warn(`Could not clear the destination on the service: ${failure}`)
    } catch (error) {
      console.warn('Could not reach the service to clear the destination', error)
    }
  }

  private derive(previous: { progressM: number; headingDeg: number; arrived: boolean }): DriveState {
    return deriveState({
      socket: this.socket,
      status: this.status,
      ipadPosition: this.ipadPosition,
      defaultPosition: DEFAULT_POSITION,
      trip: this.trip,
      progressM: previous.progressM,
      previousHeadingDeg: previous.headingDeg,
      arrived: previous.arrived,
      linkLost: this.linkLost,
      linkIssue: this.linkIssue,
      // Undefined only while the constructor derives the first state.
      previousMode: this.state?.mode,
    })
  }

  /**
   * Re-derives the state; a new trip passes `progressM: 0, arrived: false` to start afresh.
   * Subscribers hear only of real changes: most of the ~30 statuses a second repeat
   * the last one, and each notification re-renders the whole app.
   */
  private update(reset: { progressM?: number; arrived?: boolean } = {}) {
    const next = this.derive({
      progressM: reset.progressM ?? this.state.progressM,
      headingDeg: this.state.headingDeg,
      arrived: reset.arrived ?? this.state.mode === 'arrived',
    })
    if (sameValue(next, this.state)) return
    this.state = next
    this.listeners.forEach((fn) => fn())
  }
}
