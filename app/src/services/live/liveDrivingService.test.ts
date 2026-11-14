import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LatLng } from '../../types'
import type { CameraFrame } from '../drivingService'
import { ServiceRefusedError } from '../errors'
import { LiveDrivingService, type LiveOptions } from './liveDrivingService'
import { FakeSocket, ManualTimers, lRoute } from './testing'

const ok = () => new Response(JSON.stringify({ success: true, data: null, error: null }))

function setup(options: Partial<LiveOptions> = {}) {
  const sockets: FakeSocket[] = []
  const timers = new ManualTimers()
  const calls: { url: string; init?: RequestInit }[] = []
  const fetchFn = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    return ok()
  })
  let urls = 0
  const revoked: string[] = []
  let ipadListener: ((p: LatLng) => void) | null = null
  const { route, place } = lRoute()
  const service = new LiveDrivingService({
    socketUrl: 'ws://test/ws/viewer',
    createSocket: (url) => {
      const socket = new FakeSocket(url)
      sockets.push(socket)
      return socket
    },
    timers,
    fetchFn: fetchFn as unknown as typeof fetch,
    planRoute: async () => route,
    createObjectUrl: () => `blob:${++urls}`,
    revokeObjectUrl: (url) => revoked.push(url),
    onIpadLocation: (fn) => {
      ipadListener = fn
      return () => {
        ipadListener = null
      }
    },
    ...options,
  })
  const socket = () => sockets[sockets.length - 1]
  const online = () => {
    socket().open()
    socket().sendStatus()
  }
  return {
    service,
    socket,
    online,
    timers,
    calls,
    fetchFn,
    revoked,
    place,
    route,
    ipad: (p: LatLng) => ipadListener?.(p),
    hasIpadListener: () => ipadListener !== null,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('LiveDrivingService', () => {
  it('starts connecting, then follows the service and notifies subscribers', () => {
    const t = setup()
    expect(t.service.getState().link.service).toBe('connecting')
    const listener = vi.fn()
    const unsubscribe = t.service.subscribe(listener)
    t.online()
    expect(listener).toHaveBeenCalled()
    expect(t.service.getState()).toMatchObject({ source: 'live', connected: true, link: { phone: 'ok' } })
    unsubscribe()
    listener.mockClear()
    t.socket().sendStatus()
    expect(listener).not.toHaveBeenCalled()
  })

  it('uses the iPad’s location until the phone has a fix', () => {
    const t = setup()
    t.socket().open()
    t.socket().sendStatus({ gps: null, sensors_ok: false })
    t.ipad({ lat: 10, lng: 20 })
    expect(t.service.getState()).toMatchObject({ position: { lat: 10, lng: 20 }, link: { positionFrom: 'ipad' } })
  })

  it('forgets the phone when the service is lost', () => {
    const t = setup()
    t.online()
    t.socket().drop()
    expect(t.service.getState()).toMatchObject({ connected: false, link: { service: 'offline', phone: 'none' } })
  })

  it('sends the destination to the service and holds a trip', async () => {
    const t = setup()
    t.online()
    await t.service.setDestination(t.place)
    expect(t.calls[0].url).toBe('/api/destination')
    expect(t.calls[0].init?.method).toBe('POST')
    expect(JSON.parse(String(t.calls[0].init?.body))).toEqual({ lat: t.place.location.lat, lon: t.place.location.lng })
    expect(t.service.getState()).toMatchObject({ mode: 'ready', destination: t.place, route: t.route })
  })

  it('rejects with the service’s reason and keeps no trip when it refuses', async () => {
    const refuse = async () =>
      new Response(JSON.stringify({ success: false, data: null, error: 'request came from another site' }), { status: 403 })
    const t = setup({ fetchFn: refuse as unknown as typeof fetch })
    t.online()
    await expect(t.service.setDestination(t.place)).rejects.toThrow(ServiceRefusedError)
    expect(t.service.getState().mode).toBe('idle')
  })

  it('reports the HTTP status for a non-JSON failure and a network error', async () => {
    const page = async () => new Response('<html>', { status: 502 })
    const a = setup({ fetchFn: page as unknown as typeof fetch })
    await expect(a.service.setDestination(a.place)).rejects.toThrow(/HTTP 502/)
    const down = async () => {
      throw new TypeError('Load failed')
    }
    const b = setup({ fetchFn: down as unknown as typeof fetch })
    await expect(b.service.setDestination(b.place)).rejects.toThrow(/Load failed/)
  })

  it('drops a destination that was superseded while it was being planned', async () => {
    let finish: (() => void) | null = null
    const { route } = lRoute()
    const slow = () =>
      new Promise<typeof route>((resolve) => {
        finish = () => resolve(route)
      })
    const t = setup({ planRoute: slow })
    const first = t.service.setDestination(t.place)
    t.service.clearDestination()
    finish!()
    await first
    expect(t.service.getState().mode).toBe('idle')
    expect(t.calls.map((c) => c.init?.method)).toEqual(['DELETE'])
  })

  it('clears the destination on the service when the trip ends or goes indoors', async () => {
    const t = setup()
    t.online()
    await t.service.setDestination(t.place)
    await t.service.setLanguageGoal('go to the kitchen')
    expect(t.service.getState()).toMatchObject({ mode: 'ready', languageGoal: 'go to the kitchen', destination: null })
    t.service.clearDestination()
    await Promise.resolve()
    expect(t.service.getState().mode).toBe('idle')
    expect(t.calls.map((c) => c.init?.method)).toEqual(['POST', 'DELETE', 'DELETE'])
  })

  it('warns, without throwing, when clearing on the service fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const refuse = async () => new Response(JSON.stringify({ success: false, data: null, error: 'busy' }), { status: 429 })
    const t = setup({ fetchFn: refuse as unknown as typeof fetch })
    await t.service.setLanguageGoal('go to the lift')
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('busy'))
    const down = async () => {
      throw new Error('offline')
    }
    const u = setup({ fetchFn: down as unknown as typeof fetch })
    await u.service.setLanguageGoal('go to the lift')
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('clears a destination the service still holds from before the iPad reloaded', async () => {
    const t = setup()
    t.socket().open()
    t.socket().sendStatus({ destination: { lat: 1, lon: 2, distance_m: null, bearing_deg: null } })
    t.socket().sendStatus({ destination: { lat: 1, lon: 2, distance_m: null, bearing_deg: null } })
    await Promise.resolve()
    // Once per connection, not on every status.
    expect(t.calls.map((c) => c.init?.method)).toEqual(['DELETE'])
  })

  it('leaves the service alone when neither side has a destination', async () => {
    const t = setup()
    t.online()
    await Promise.resolve()
    expect(t.calls).toEqual([])
  })

  it('resends the destination after reconnecting, since the service keeps it in memory only', async () => {
    const t = setup()
    t.online()
    await t.service.setDestination(t.place)
    t.socket().drop()
    t.timers.fire(500)
    t.online()
    await Promise.resolve()
    expect(t.calls.map((c) => c.init?.method)).toEqual(['POST', 'POST'])
  })

  it('never engages: self-driving is not available from the service yet', () => {
    const t = setup()
    t.online()
    t.service.engage()
    t.service.disengage()
    expect(t.service.getState().mode).toBe('idle')
    expect(t.service.getState().engageBlocked).not.toBeNull()
  })

  it('hands camera frames to subscribers only, and revokes old frame URLs', async () => {
    const t = setup()
    t.online()
    t.socket().receive(new ArrayBuffer(8)) // nobody watching: not decoded
    const feed = await t.service.openCamera()
    expect(feed.kind).toBe('frames')
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    const stop = feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    t.socket().receive(new ArrayBuffer(8))
    t.socket().receive(new ArrayBuffer(8))
    // First: no picture yet, but a live one is coming.
    expect(frames.map((f) => f.url)).toEqual([null, 'blob:1', 'blob:2', 'blob:3'])
    expect(frames.every((f) => f.fresh)).toBe(true)
    expect(t.revoked).toEqual(['blob:1'])
    // A late subscriber gets the current frame straight away.
    const late: CameraFrame[] = []
    const stopLate = feed.subscribe((f) => late.push(f))
    expect(late.map((f) => f.url)).toEqual(['blob:3'])
    stopLate()
    stop()
    expect(t.revoked).toEqual(['blob:1', 'blob:2', 'blob:3'])
  })

  it('marks the picture stale when frames stop or the service is lost, without a new frame', async () => {
    const t = setup()
    t.online()
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    t.socket().sendStatus({ frame: { seq: 7, fresh: false } })
    t.socket().sendStatus({ frame: { seq: 7, fresh: false } })
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: false })
    // Pushed once per change, not on every status.
    expect(frames.filter((f) => !f.fresh)).toHaveLength(1)
    t.socket().sendStatus()
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: true })
    t.socket().drop()
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: false })
  })

  it('says straight away when no live picture is coming', async () => {
    const t = setup()
    t.socket().open()
    t.socket().sendStatus({ phone_connected: false, sensors_ok: false, frame: null })
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    expect(frames).toEqual([{ url: null, fresh: false }])
  })

  it('keeps the connection lost through retries until a status arrives, with the reason', () => {
    const t = setup()
    t.online()
    t.socket().drop(4429)
    t.timers.fire(500)
    t.socket().open()
    expect(t.service.getState()).toMatchObject({ connected: false, link: { issue: 'too many screens are connected' } })
    t.socket().sendStatus()
    expect(t.service.getState()).toMatchObject({ connected: true, link: { issue: null } })
  })

  it('keeps arrival until the trip changes', async () => {
    const t = setup()
    t.online()
    await t.service.setDestination(t.place)
    const { lat, lng } = t.place.location
    t.socket().sendStatus({ gps: { lat, lon: lng, accuracy_m: 4, speed_mps: 0, course_deg: null, fresh: true } })
    expect(t.service.getState().mode).toBe('arrived')
    t.socket().sendStatus()
    expect(t.service.getState().mode).toBe('arrived')
    await t.service.setDestination(t.place)
    expect(t.service.getState().mode).toBe('ready')
  })

  it('warns when resending the destination after a reconnect fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    let fail = false
    const fetchFn = async () =>
      fail
        ? new Response(JSON.stringify({ success: false, data: null, error: 'busy' }), { status: 429 })
        : new Response(JSON.stringify({ success: true, data: null, error: null }))
    const t = setup({ fetchFn: fetchFn as unknown as typeof fetch })
    t.online()
    await t.service.setDestination(t.place)
    fail = true
    t.socket().drop()
    t.timers.fire(500)
    t.online()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('busy'))
  })

  it('releases everything when the camera closes and when disposed', async () => {
    const t = setup()
    t.online()
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    feed.subscribe(() => {})
    t.socket().receive(new ArrayBuffer(8))
    t.service.closeCamera()
    expect(t.revoked).toEqual(['blob:1'])
    t.service.dispose()
    expect(t.socket().closed).toBe(true)
    expect(t.hasIpadListener()).toBe(false)
  })
})
