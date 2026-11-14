import { backoffMs, type Timers } from '../live/viewerSocket'
import {
  addMotion,
  EMPTY_BATCH,
  gpsMessage,
  headingMessage,
  helloMessage,
  type ClockSample,
  type GpsMessage,
  type ImuBatch,
  type MotionLike,
  type OrientationLike,
  pingMessage,
  type PositionLike,
  recentFix,
  sampleFromPong,
  type SensorMessage,
  takeBatch,
} from './messages'

// The iPad as the chair's sensor source: GPS, compass and motion to the driving
// service's /ws/phone, in place of the phone, while an external camera sends the
// frames on /ws/camera. Same rules as the phone's capture page
// (docs/phone-protocol.md): hello then ping on every connect, a ping every
// second, reconnect with backoff, and stop for good when another device takes
// the slot (4410) or has priority (4409).

/** Pings and the liveness check run on this tick. */
const TICK_MS = 1000
/** About five missed pongs: the link is dead even if the socket still looks open. */
export const LIVENESS_TIMEOUT_MS = 5000
const HEADING_MIN_GAP_MS = 30
const IMU_FLUSH_MS = 50
const IMU_MAX_SAMPLES = 200
const GPS_RESEND_MAX_AGE_MS = 10_000
/** The connection only counts as working (backoff reset) after this long, or a reply. */
const STABLE_AFTER_MS = 2000
const CLOSE_REPLACED = 4410
const CLOSE_BUSY = 4409

export type UplinkState = 'connecting' | 'sending' | 'retrying' | 'taken-over' | 'stopped'

/** The parts of a browser WebSocket used here, so tests can pass a fake. */
export interface UplinkSocket {
  readonly readyState: number
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onclose: ((event: CloseEvent) => void) | null
  send(data: string): void
  close(code?: number, reason?: string): void
}

type Unsubscribe = () => void

/** Where readings come from; the browser's in the app, fakes in tests. */
export interface SensorSource {
  watchPosition(onPosition: (position: PositionLike) => void, onError: (message: string) => void): Unsubscribe
  listenOrientation(onReading: (event: OrientationLike) => void): Unsubscribe
  listenMotion(onMotion: (event: MotionLike) => void): Unsubscribe
}

export interface UplinkOptions {
  url: string
  userAgent: string
  createSocket: (url: string) => UplinkSocket
  sensors: SensorSource
  timers: Timers
  now: () => number
  onState: (state: UplinkState, detail: string | null) => void
}

export interface Uplink {
  /** Stops sending and closes the socket for good. */
  close(): void
}

const OPEN = 1

/** ws:// or wss:// to /ws/phone on the page's own host (the dev server proxies it). */
export function phoneSocketUrl(location: Pick<Location, 'protocol' | 'host'>): string {
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/phone`
}

function closeReason(code: number): { final: boolean; detail: string } {
  if (code === CLOSE_REPLACED) return { final: true, detail: 'another device took over the sensor stream' }
  if (code === CLOSE_BUSY) return { final: true, detail: 'another phone has priority' }
  return { final: false, detail: 'connection lost' }
}

export function connectSensorUplink(options: UplinkOptions): Uplink {
  const { timers, now } = options
  let socket: UplinkSocket | null = null
  let attempt = 0
  let openedAt = 0
  let lastHeard = 0
  let lastFix: GpsMessage | null = null
  let lastHeadingAt = Number.NEGATIVE_INFINITY
  let clock: ClockSample | null = null
  let imu: ImuBatch = EMPTY_BATCH
  let retryTimer: number | null = null
  let tickTimer: number | null = null
  let stopped = false

  const send = (message: SensorMessage) => {
    if (socket !== null && socket.readyState === OPEN) socket.send(JSON.stringify(message))
  }

  const detach = (current: UplinkSocket) => {
    current.onopen = current.onmessage = current.onclose = null
    if (tickTimer !== null) timers.clear(tickTimer)
    tickTimer = null
    socket = null
    clock = null
  }

  const lose = (current: UplinkSocket, code: number) => {
    if (stopped || socket !== current) return
    detach(current)
    current.close()
    const { final, detail } = closeReason(code)
    if (final) {
      options.onState('taken-over', detail)
      return
    }
    if (now() - openedAt >= STABLE_AFTER_MS) attempt = 0
    options.onState('retrying', detail)
    retryTimer = timers.set(open, backoffMs(attempt))
    attempt += 1
  }

  const tick = (current: UplinkSocket) => {
    if (now() - lastHeard > LIVENESS_TIMEOUT_MS) {
      lose(current, 4000)
      return
    }
    send(pingMessage(now(), clock))
    tickTimer = timers.set(() => tick(current), TICK_MS)
  }

  const onMessage = (event: MessageEvent) => {
    lastHeard = now()
    attempt = 0
    if (typeof event.data !== 'string') return
    let message: unknown
    try {
      message = JSON.parse(event.data)
    } catch {
      return
    }
    if (typeof message !== 'object' || message === null) return
    const { type, t, server_t: serverT } = message as Record<string, unknown>
    if (type === 'pong' && typeof t === 'number' && typeof serverT === 'number') {
      clock = sampleFromPong({ t, server_t: serverT }, now()) ?? clock
    }
  }

  function open() {
    retryTimer = null
    const current = options.createSocket(options.url)
    socket = current
    openedAt = now()
    lastHeard = openedAt
    options.onState('connecting', null)
    current.onopen = () => {
      // The service forgets this device and its clock on every connect.
      send(helloMessage(now(), options.userAgent))
      send(pingMessage(now(), clock))
      const fix = recentFix(lastFix, now(), GPS_RESEND_MAX_AGE_MS)
      if (fix !== null) send(fix)
      options.onState('sending', null)
      tickTimer = timers.set(() => tick(current), TICK_MS)
    }
    current.onmessage = onMessage
    current.onclose = (event) => lose(current, event.code)
  }

  const unsubscribers = [
    options.sensors.watchPosition(
      (position) => {
        lastFix = gpsMessage(position, now())
        send(lastFix)
      },
      (message) => options.onState(socket === null ? 'retrying' : 'sending', message),
    ),
    options.sensors.listenOrientation((event) => {
      const t = now()
      if (t - lastHeadingAt < HEADING_MIN_GAP_MS) return
      const message = headingMessage(event, t)
      if (message === null) return
      lastHeadingAt = t
      send(message)
    }),
    options.sensors.listenMotion((event) => {
      const t = now()
      const result = takeBatch(addMotion(imu, event, t), t, IMU_FLUSH_MS, IMU_MAX_SAMPLES)
      imu = result.batch
      if (result.message !== null) send(result.message)
    }),
  ]

  open()
  return {
    close() {
      if (stopped) return
      stopped = true
      unsubscribers.forEach((unsubscribe) => unsubscribe())
      if (retryTimer !== null) timers.clear(retryTimer)
      retryTimer = null
      const current = socket
      if (current !== null) {
        detach(current)
        current.close(1000, 'iPad stopped sending')
      }
      options.onState('stopped', null)
    },
  }
}
