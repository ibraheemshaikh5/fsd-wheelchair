import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LatLng } from '../../types'
import type { CameraFrame } from '../drivingService'
import { ServiceRefusedError } from '../errors'
import {
  DISPLAY_FRAME_FRESH_MS,
  DISPLAY_FRAME_MAX_AGE_MS,
  FRAME_URLS_KEPT,
  LiveDrivingService,
  type LiveOptions,
 routePoints } from './liveDrivingService'
import { FakeSocket, ManualTimers, lRoute, status } from './testing'

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
  const clock = { ms: 1_000_000 }
  const { route, place } = lRoute()
  const service = new LiveDrivingService({
    now: () => clock.ms,
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
    clock,
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
    // The destination and its route, then the instruction in place of both, then the clear.
    expect(t.calls.map((c) => `${c.init?.method} ${c.url}`)).toEqual([
      'POST /api/destination',
      'POST /api/goal',
      'POST /api/goal',
      'DELETE /api/destination',
    ])
    const route = JSON.parse(String(t.calls[1].init?.body))
    expect(route.kind).toBe('route')
    expect(route.points).toEqual(t.route.points.map((p) => ({ lat: p.lat, lon: p.lng })))
    expect(JSON.parse(String(t.calls[2].init?.body))).toEqual({ kind: 'instruction', text: 'go to the kitchen' })
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
    // Each time: the instruction could not be sent, then nor could the fallback clear.
    expect(warn).toHaveBeenCalledTimes(4)
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
    await vi.waitFor(() => expect(t.calls).toHaveLength(4))
    // Destination and route when set, then both again after the reconnect.
    expect(t.calls.map((c) => c.url)).toEqual(['/api/destination', '/api/goal', '/api/destination', '/api/goal'])
  })

  it('resends an indoor instruction after reconnecting', async () => {
    const t = setup()
    t.online()
    await t.service.setLanguageGoal('go to the kitchen')
    t.socket().drop()
    t.timers.fire(500)
    t.online()
    await vi.waitFor(() => expect(t.calls).toHaveLength(2))
    expect(JSON.parse(String(t.calls[1].init?.body))).toEqual({ kind: 'instruction', text: 'go to the kitchen' })
  })

  it('thins a route longer than the service takes, keeping its end', () => {
    const points = Array.from({ length: 5001 }, (_, i) => ({ lat: i / 1e5, lng: 0 }))
    const sent = routePoints({ points, steps: [], lengthM: 0 })
    expect(sent.length).toBeLessThanOrEqual(2000)
    expect(sent[0]).toEqual({ lat: 0, lon: 0 })
    expect(sent.at(-1)).toEqual({ lat: 0.05, lon: 0 })
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
    const count = FRAME_URLS_KEPT + 3
    for (let i = 0; i < count; i++) t.socket().receive(new ArrayBuffer(8))
    // First: no picture yet, but one has just arrived, so a live one is coming.
    expect(frames.map((f) => f.url)).toEqual([null, ...Array.from({ length: count }, (_, i) => `blob:${i + 1}`)])
    expect(frames.every((f) => f.fresh)).toBe(true)
    // At 30 fps React may skip rendering some frames, so a few recent URLs stay
    // valid while the image swaps; anything older is revoked.
    expect(t.revoked).toEqual(['blob:1', 'blob:2', 'blob:3'])
    // A late subscriber gets the current frame straight away.
    const late: CameraFrame[] = []
    const stopLate = feed.subscribe((f) => late.push(f))
    expect(late.map((f) => f.url)).toEqual([`blob:${count}`])
    stopLate()
    stop()
    expect(t.revoked).toHaveLength(count)
  })

  it('re-renders only when a status changes what the rider sees', () => {
    // Statuses arrive about 30 times a second; most repeat the last one.
    const t = setup()
    t.online()
    const listener = vi.fn()
    t.service.subscribe(listener)
    const before = t.service.getState()
    t.socket().sendStatus({ frame: { seq: 8, fresh: true } })
    t.socket().sendStatus({ frame: { seq: 9, fresh: true } })
    expect(listener).not.toHaveBeenCalled()
    expect(t.service.getState()).toBe(before)
    const motion = { speed_mps: 0.8, speed_source: 'imu', yaw_rate_dps: 0, accel_fwd_mps2: 0, accel_lat_mps2: 0, age_ms: 10, fresh: true }
    t.socket().sendStatus({ motion })
    expect(listener).toHaveBeenCalledTimes(1)
    expect(t.service.getState().speedMps).toBeCloseTo(0.8, 1)
  })

  it('does not re-render for the frame-rate, speed and compass jitter of a streaming phone', () => {
    // The service's fps is a float over a sliding window and IMU speed moves a
    // little with every batch, so nearly every real status differs from the last.
    const t = setup()
    t.online()
    const motion = (speed_mps: number) => ({ ...(status().motion as object), speed_mps, speed_source: 'imu' })
    const reading = (fps: number, speed: number, deg: number) => ({
      stats: { fps, frames: 100, missed: 0, rejected: 0, rate_limited: 0 },
      motion: motion(speed),
      heading: { deg, source: 'compass', reference: 'true' },
    })
    t.socket().sendStatus(reading(30, 0.8, 90))
    const listener = vi.fn()
    t.service.subscribe(listener)
    t.socket().sendStatus(reading(29.7, 0.81, 90.3))
    t.socket().sendStatus(reading(30.1, 0.79, 89.8))
    t.socket().sendStatus(reading(30.4, 0.8, 90.1))
    expect(listener).not.toHaveBeenCalled()
  })

  it('marks the picture stale on the next status once frames stop arriving, without a new frame', async () => {
    const t = setup()
    t.online()
    t.socket().receive(new ArrayBuffer(8)) // frames already flowing, not yet watched
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    t.clock.ms += DISPLAY_FRAME_FRESH_MS
    t.socket().sendStatus()
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: true, delayed: false })
    // The service still calls its frame fresh, but nothing has reached the iPad for over a second.
    t.clock.ms += 1
    t.socket().sendStatus()
    t.socket().sendStatus()
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: false, delayed: false })
    // Pushed once per change, not on every status.
    expect(frames.filter((f) => !f.fresh)).toHaveLength(1)
    t.socket().receive(new ArrayBuffer(8))
    expect(frames.at(-1)).toEqual({ url: 'blob:2', fresh: true, delayed: false })
    t.socket().drop()
    expect(frames.at(-1)).toEqual({ url: 'blob:2', fresh: false, delayed: false })
  })

  it('keeps showing a live picture while frames arrive, even when the service calls them stale', async () => {
    // The service's freshness is the strict drive-safety one (500 ms from capture,
    // needs clock sync): a network hiccup or clock re-sync must not blank the view.
    const t = setup()
    t.online()
    t.socket().receive(new ArrayBuffer(8)) // frames already flowing, not yet watched
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    t.socket().sendStatus({ clock_synced: false, sensors_ok: false, frame: { seq: 8, fresh: false } })
    t.clock.ms += 400
    t.socket().receive(new ArrayBuffer(8))
    t.socket().sendStatus({ clock_synced: false, sensors_ok: false, frame: { seq: 9, fresh: false } })
    expect(frames.every((f) => f.fresh)).toBe(true)
    // Driving-related readings still follow the strict status.
    expect(t.service.getState().link.phone).toBe('waiting')
  })

  it('says the picture is delayed when frames keep arriving but were captured long ago', async () => {
    const t = setup()
    t.online()
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: true, delayed: false })
    // A backlog or slow link: frames reach the iPad, but seconds after capture.
    t.socket().sendStatus({ frame: { seq: 8, age_ms: 3000, fresh: false } })
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: true, delayed: true })
    t.socket().receive(new ArrayBuffer(8))
    expect(frames.at(-1)).toEqual({ url: 'blob:2', fresh: true, delayed: true })
    t.socket().sendStatus({ frame: { seq: 9, age_ms: DISPLAY_FRAME_MAX_AGE_MS, fresh: false } })
    expect(frames.at(-1)).toEqual({ url: 'blob:2', fresh: true, delayed: false })
  })

  it('cannot tell a delay before the phone clock is synced, or when the age is unknown', async () => {
    const t = setup()
    t.online()
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    t.socket().sendStatus({ clock_synced: false, sensors_ok: false, frame: { seq: 8, age_ms: 3000, fresh: false } })
    t.socket().sendStatus({ frame: { seq: 9, age_ms: null, fresh: false } })
    expect(frames.every((f) => !f.delayed)).toBe(true)
  })

  it('does not count frames from before a lost connection as live after reconnecting', async () => {
    const t = setup()
    t.online()
    t.socket().receive(new ArrayBuffer(8))
    t.socket().drop()
    t.timers.fire(500)
    t.online()
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    expect(frames).toEqual([{ url: null, fresh: false, delayed: false }])
  })

  it('does not show the old frame the service replays on connect as live once the phone has gone', async () => {
    // The service keeps the phone's last frame after it disconnects and sends it to
    // every viewer that connects, however old it is.
    const t = setup()
    t.socket().open()
    t.socket().sendStatus({ phone_connected: false, clock_synced: false, sensors_ok: false, frame: { seq: 7, age_ms: 600_000, fresh: false } })
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8))
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: false, delayed: false })
  })

  it('waits for a second frame before trusting a replayed one while the phone’s camera may have stopped', async () => {
    const t = setup()
    t.socket().open()
    t.socket().sendStatus({ frame: { seq: 7, age_ms: 600_000, fresh: false } })
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    t.socket().receive(new ArrayBuffer(8)) // the replay
    expect(frames.at(-1)).toEqual({ url: 'blob:1', fresh: false, delayed: false })
    // A new frame proves pictures are flowing, even if the strict status still says stale.
    t.socket().sendStatus({ clock_synced: false, sensors_ok: false, frame: { seq: 8, age_ms: null, fresh: false } })
    t.socket().receive(new ArrayBuffer(8))
    expect(frames.at(-1)).toEqual({ url: 'blob:2', fresh: true, delayed: false })
  })

  it('never says delayed on the way to no live picture when frames stop', async () => {
    // age_ms counts from capture and keeps growing once frames stop; the iPad's own
    // freshness counts from receipt. Only a frame that was old on arrival is delayed.
    const t = setup()
    t.online()
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    const transitMs = 150
    t.socket().sendStatus({ frame: { seq: 8, age_ms: transitMs, fresh: true } })
    t.socket().receive(new ArrayBuffer(8))
    for (let elapsed = 33; elapsed <= DISPLAY_FRAME_FRESH_MS + 100; elapsed += 33) {
      t.clock.ms += 33
      t.socket().sendStatus({ frame: { seq: 8, age_ms: transitMs + elapsed, fresh: transitMs + elapsed <= 500 } })
    }
    expect(frames.some((f) => f.delayed)).toBe(false)
    expect(frames.at(-1)?.fresh).toBe(false)
  })

  it('says straight away when no live picture is coming', async () => {
    const t = setup()
    t.socket().open()
    t.socket().sendStatus({ phone_connected: false, sensors_ok: false, frame: null })
    const feed = await t.service.openCamera()
    if (feed.kind !== 'frames') return
    const frames: CameraFrame[] = []
    feed.subscribe((f) => frames.push(f))
    expect(frames).toEqual([{ url: null, fresh: false, delayed: false }])
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
