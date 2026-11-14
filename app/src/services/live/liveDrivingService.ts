import type { DriveState, LatLng, Place } from '../../types'
import { DEFAULT_POSITION, onDeviceLocation } from '../../lib/deviceLocation'
import type { CameraFeed, CameraFrame, DrivingService } from '../drivingService'
import { ServiceRefusedError } from '../errors'
import { planRoute } from '../routing'
import { deriveState, type Trip } from './liveState'
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
 * The real driving service, over its /ws/viewer socket and /api/destination.
 *
 * The service streams the phone's camera and sensors and keeps the destination;
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
  /** The camera freshness last told to the camera view, so changes are pushed once. */
  private cameraWasFresh: boolean | null = null
  private state: DriveState
  private readonly listeners = new Set<() => void>()
  private readonly cameraListeners = new Set<(frame: CameraFrame) => void>()
  /** The two newest frame URLs: the one on screen and the one replacing it. */
  private frameUrls: string[] = []
  private readonly connection: ViewerConnection
  private readonly stopIpadLocation: () => void
  private readonly fetchFn: typeof fetch
  private readonly plan: typeof planRoute
  private readonly createObjectUrl: (blob: Blob) => string
  private readonly revokeObjectUrl: (url: string) => void

  constructor(options: LiveOptions = {}) {
    this.fetchFn = options.fetchFn ?? ((input, init) => fetch(input, init))
    this.plan = options.planRoute ?? planRoute
    this.createObjectUrl = options.createObjectUrl ?? ((blob) => URL.createObjectURL(blob))
    this.revokeObjectUrl = options.revokeObjectUrl ?? ((url) => URL.revokeObjectURL(url))
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
  }

  async setLanguageGoal(instruction: string): Promise<void> {
    this.planning++
    // Indoor goals stay on the iPad for now; the service only takes map points.
    this.trip = { kind: 'indoor', instruction }
    this.update({ progressM: 0, arrived: false })
    await this.clearServiceDestination()
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
        listener({ url: this.frameUrls.at(-1) ?? null, fresh: this.cameraFresh() })
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
    if (socket !== 'online') this.status = null
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
      const { place } = this.trip
      void this.sendDestination(place).then((failure) => {
        if (!failure) return
        console.warn(`Could not resend the destination to the service: ${failure}`)
        // Try again on the next connection; retrying on every status would flood it.
      })
    } else if (status.destination) {
      void this.clearServiceDestination()
    }
  }

  /** A live picture: the iPad hears the service and the service has a fresh frame. */
  private cameraFresh(): boolean {
    return this.socket === 'online' && !this.linkLost && (this.status?.frame?.fresh ?? false)
  }

  /** Frames stop arriving when the phone does, so freshness changes are pushed from the status. */
  private notifyCamera() {
    if (this.cameraListeners.size === 0) return
    const fresh = this.cameraFresh()
    if (fresh === this.cameraWasFresh) return
    this.cameraWasFresh = fresh
    const frame = { url: this.frameUrls.at(-1) ?? null, fresh }
    this.cameraListeners.forEach((fn) => fn(frame))
  }

  private onFrame(jpeg: ArrayBuffer) {
    // Frames arrive whether or not anyone is looking; only decode them for the camera view.
    if (this.cameraListeners.size === 0) return
    const url = this.createObjectUrl(new Blob([jpeg], { type: 'image/jpeg' }))
    this.frameUrls.push(url)
    // Keep the one still on screen until its replacement has been handed out.
    while (this.frameUrls.length > 2) this.revokeObjectUrl(this.frameUrls.shift() as string)
    const fresh = this.cameraFresh()
    this.cameraWasFresh = fresh
    const frame = { url, fresh }
    this.cameraListeners.forEach((fn) => fn(frame))
  }

  private releaseFrames() {
    this.frameUrls.forEach((url) => this.revokeObjectUrl(url))
    this.frameUrls = []
    this.cameraWasFresh = null
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
    })
  }

  /** Re-derives the state; a new trip passes `progressM: 0, arrived: false` to start afresh. */
  private update(reset: { progressM?: number; arrived?: boolean } = {}) {
    this.state = this.derive({
      progressM: reset.progressM ?? this.state.progressM,
      headingDeg: this.state.headingDeg,
      arrived: reset.arrived ?? this.state.mode === 'arrived',
    })
    this.listeners.forEach((fn) => fn())
  }
}
