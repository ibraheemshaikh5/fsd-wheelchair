import { beforeEach, describe, expect, it } from 'vitest'
import { ManualTimers } from '../live/testing'
import {
  addMotion,
  EMPTY_BATCH,
  gpsMessage,
  headingMessage,
  helloMessage,
  type MotionLike,
  type OrientationLike,
  pingMessage,
  type PositionLike,
  recentFix,
  sampleFromPong,
  takeBatch,
} from './messages'
import { connectSensorUplink, LIVENESS_TIMEOUT_MS, phoneSocketUrl, type UplinkSocket, type UplinkState } from './uplink'

class FakeSocket implements UplinkSocket {
  readyState = 0
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  sent: Record<string, unknown>[] = []
  closed = false
  constructor(readonly url: string) {}
  open() {
    this.readyState = 1
    this.onopen?.({} as Event)
  }
  receive(message: unknown) {
    this.onmessage?.({ data: typeof message === 'string' ? message : JSON.stringify(message) } as MessageEvent)
  }
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code } as CloseEvent)
  }
  send(data: string) {
    this.sent.push(JSON.parse(data) as Record<string, unknown>)
  }
  close() {
    this.closed = true
  }
  types() {
    return this.sent.map((m) => m.type)
  }
}

class FakeSensors {
  position: ((p: PositionLike) => void) | null = null
  positionError: ((m: string) => void) | null = null
  orientation: ((e: OrientationLike) => void) | null = null
  motion: ((e: MotionLike) => void) | null = null
  unsubscribed = 0
  watchPosition = (fn: (p: PositionLike) => void, onError: (m: string) => void) => {
    this.position = fn
    this.positionError = onError
    return () => this.unsubscribed++
  }
  listenOrientation = (fn: (e: OrientationLike) => void) => {
    this.orientation = fn
    return () => this.unsubscribed++
  }
  listenMotion = (fn: (e: MotionLike) => void) => {
    this.motion = fn
    return () => this.unsubscribed++
  }
}

const position = (timestamp = 1_000): PositionLike => ({
  coords: { latitude: 38.98, longitude: -76.94, accuracy: 5, altitude: 40, speed: -1, heading: null },
  timestamp,
})

const motion = (x = 0.1): MotionLike => ({
  accelerationIncludingGravity: { x, y: 9.8, z: 0.2 },
  rotationRate: { alpha: 1, beta: 2, gamma: 3 },
})

describe('sensor messages', () => {
  it('says hello with no camera and device axes', () => {
    expect(helloMessage(5, 'iPad')).toEqual({
      v: 1,
      type: 'hello',
      t: 5,
      user_agent: 'iPad',
      camera: null,
      mount: null,
      imu_axes: 'device_xyz',
    })
  })

  it('turns a position into a gps message and drops Safari placeholders', () => {
    const fix = gpsMessage(position(900), 1_000)
    expect(fix).toMatchObject({ lat: 38.98, lon: -76.94, accuracy_m: 5, speed_mps: null, course_deg: null, fix_t: 900 })
    expect(gpsMessage({ ...position(0), coords: { ...position().coords, heading: 370, speed: 1 } }, 7)).toMatchObject({
      course_deg: 10,
      speed_mps: 1,
      fix_t: 7,
    })
  })

  it('resends only a recent fix, restamped', () => {
    const fix = gpsMessage(position(1_000), 1_000)
    expect(recentFix(fix, 5_000, 10_000)?.t).toBe(5_000)
    expect(recentFix(fix, 20_000, 10_000)).toBeNull()
    expect(recentFix(null, 1, 1)).toBeNull()
  })

  it('reads the Safari compass, or an absolute alpha', () => {
    const safari = { webkitCompassHeading: 370, webkitCompassAccuracy: -1, alpha: null, absolute: false }
    expect(headingMessage(safari, 1)).toMatchObject({ magnetic_deg: 10, accuracy_deg: null })
    expect(headingMessage({ alpha: 90, absolute: true }, 1)).toMatchObject({ magnetic_deg: 270 })
    expect(headingMessage({ alpha: 90, absolute: false }, 1)).toBeNull()
  })

  it('batches motion and emits once old enough', () => {
    let batch = addMotion(EMPTY_BATCH, motion(), 100)
    batch = addMotion(batch, motion(0.2), 116)
    expect(addMotion(batch, { accelerationIncludingGravity: null, rotationRate: null }, 120)).toBe(batch)
    expect(takeBatch(batch, 120, 50, 200).message).toBeNull()
    const result = takeBatch(batch, 150, 50, 200)
    expect(result.message).toEqual({
      v: 1,
      type: 'imu',
      t: 100,
      samples: [
        [0, 0.1, 9.8, 0.2, 1, 2, 3],
        [16, 0.2, 9.8, 0.2, 1, 2, 3],
      ],
    })
    expect(result.batch).toBe(EMPTY_BATCH)
    expect(takeBatch(EMPTY_BATCH, 1, 1, 1).message).toBeNull()
  })

  it('measures the clock from a pong', () => {
    expect(sampleFromPong({ t: 1000, server_t: 1510 }, 1020)).toEqual({ rtt_ms: 20, offset_ms: 500 })
    expect(sampleFromPong({ t: 1000, server_t: 0 }, 900)).toBeNull()
    expect(pingMessage(3, { rtt_ms: 20, offset_ms: 500 })).toMatchObject({ rtt_ms: 20, offset_ms: 500 })
    expect(pingMessage(3, null)).toMatchObject({ rtt_ms: null, offset_ms: null })
  })
})

describe('sensor uplink', () => {
  let timers: ManualTimers
  let sensors: FakeSensors
  let sockets: FakeSocket[]
  let states: [UplinkState, string | null][]
  let clock: number

  beforeEach(() => {
    timers = new ManualTimers()
    sensors = new FakeSensors()
    sockets = []
    states = []
    clock = 1_000
  })

  const start = () =>
    connectSensorUplink({
      url: 'wss://mac.ts.net/ws/phone',
      userAgent: 'iPad',
      createSocket: (url) => {
        const socket = new FakeSocket(url)
        sockets.push(socket)
        return socket
      },
      sensors,
      timers,
      now: () => clock,
      onState: (state, detail) => states.push([state, detail]),
    })

  it('builds the socket url from the page', () => {
    expect(phoneSocketUrl({ protocol: 'https:', host: 'mac.ts.net' })).toBe('wss://mac.ts.net/ws/phone')
    expect(phoneSocketUrl({ protocol: 'http:', host: '127.0.0.1:5174' })).toBe('ws://127.0.0.1:5174/ws/phone')
  })

  it('says hello, pings, and resends the last fix on connect', () => {
    start()
    sensors.position?.(position(900))
    expect(sockets[0].sent).toEqual([]) // not open yet
    sockets[0].open()
    expect(sockets[0].types()).toEqual(['hello', 'ping', 'gps'])
    expect(states.at(-1)).toEqual(['sending', null])
  })

  it('sends readings while open, compass at most every 30 ms', () => {
    start()
    sockets[0].open()
    sensors.position?.(position())
    const compass = { webkitCompassHeading: 45, alpha: null, absolute: false }
    sensors.orientation?.(compass)
    clock += 10
    sensors.orientation?.(compass)
    clock += 30
    sensors.orientation?.(compass)
    sensors.motion?.(motion())
    clock += 60
    sensors.motion?.(motion())
    expect(sockets[0].types()).toEqual(['hello', 'ping', 'gps', 'heading', 'heading', 'imu'])
  })

  it('pings each second with the measured clock', () => {
    start()
    sockets[0].open()
    clock += 20
    sockets[0].receive({ type: 'pong', t: 1_000, server_t: 1_510 })
    sockets[0].receive('not json')
    sockets[0].receive('42')
    timers.fire(1000)
    expect(sockets[0].sent.at(-1)).toMatchObject({ type: 'ping', rtt_ms: 20, offset_ms: 500 })
  })

  it('reconnects with backoff after a drop or a silent service', () => {
    start()
    sockets[0].open()
    sockets[0].drop()
    expect(states.at(-1)).toEqual(['retrying', 'connection lost'])
    timers.fire(500)
    sockets[1].drop()
    expect(timers.delays()).toEqual([1000]) // a quick failure waits longer
    timers.fire(1000)
    sockets[2].open()
    clock += LIVENESS_TIMEOUT_MS + 1
    timers.fire(1000)
    expect(sockets[2].closed).toBe(true)
    expect(timers.delays()).toEqual([500]) // it worked for a while: retry quickly
  })

  it('stops for good when another device takes over', () => {
    start()
    sockets[0].open()
    sockets[0].drop(4410)
    expect(states.at(-1)).toEqual(['taken-over', 'another device took over the sensor stream'])
    expect(timers.delays()).toEqual([])
    sensors.position?.(position())
    expect(sockets).toHaveLength(1)
  })

  it('reports a location error without dropping the link', () => {
    start()
    sensors.positionError?.('No location fix available yet.')
    expect(states.at(-1)).toEqual(['sending', 'No location fix available yet.'])
  })

  it('close stops sensors, timers and the socket', () => {
    const uplink = start()
    sockets[0].open()
    uplink.close()
    uplink.close()
    expect(sensors.unsubscribed).toBe(3)
    expect(sockets[0].closed).toBe(true)
    expect(timers.delays()).toEqual([])
    expect(states.at(-1)).toEqual(['stopped', null])
    sockets[0].drop()
    expect(sockets).toHaveLength(1)
  })

  it('close while retrying cancels the retry', () => {
    const uplink = start()
    sockets[0].drop(4410 - 1)
    uplink.close()
    expect(timers.delays()).toEqual([])
  })
})
