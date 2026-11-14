import { describe, expect, it } from 'vitest'
import { offset } from '../../lib/geo'
import { ARRIVE_RADIUS_M, chairLink, chairPose, deriveState, type LiveInputs, progressAlong } from './liveState'
import { ORIGIN, lRoute, status } from './testing'

const DEFAULT = { lat: 1, lng: 2 }
const IPAD = { lat: 3, lng: 4 }

function inputs(overrides: Partial<LiveInputs> = {}): LiveInputs {
  return {
    socket: 'online',
    status: status(),
    ipadPosition: null,
    defaultPosition: DEFAULT,
    trip: null,
    progressM: 0,
    previousHeadingDeg: 0,
    arrived: false,
    linkLost: false,
    linkIssue: null,
    ...overrides,
  }
}

const gps = (overrides: Record<string, unknown>) => ({ ...(status().gps as object), ...overrides })

describe('chairPose', () => {
  it('uses the phone fix, the service heading and GPS speed (a service without motion)', () => {
    const pose = chairPose(status({ motion: null, gps: gps({ speed_mps: 1.2 }) }), IPAD, DEFAULT, 0)
    expect(pose).toEqual({
      position: ORIGIN,
      positionFrom: 'phone',
      measured: true,
      accuracyM: 4,
      headingDeg: 90,
      speedMps: expect.closeTo(1.2, 1),
    })
  })

  it('takes speed from fresh IMU motion over GPS, which lags', () => {
    const motion = { ...(status().motion as object), speed_mps: 0.9, speed_source: 'imu', fresh: true }
    expect(chairPose(status({ motion, gps: gps({ speed_mps: 1.5 }) }), null, DEFAULT, 0).speedMps).toBeCloseTo(0.9, 1)
  })

  it('falls back to the fresh GPS speed when motion is stale, unknown or absent', () => {
    const base = status().motion as object
    const withGps = (motion: unknown) => status({ motion, gps: gps({ speed_mps: 1.5 }) })
    expect(chairPose(withGps({ ...base, speed_mps: 0.9, fresh: false }), null, DEFAULT, 0).speedMps).toBeCloseTo(1.5, 1)
    expect(chairPose(withGps({ ...base, speed_mps: null, fresh: true }), null, DEFAULT, 0).speedMps).toBeCloseTo(1.5, 1)
    expect(chairPose(withGps(null), null, DEFAULT, 0).speedMps).toBeCloseTo(1.5, 1)
  })

  it('counts fresh motion speed as moving, for the manual-driving mode', () => {
    const { route, place } = lRoute()
    const motion = { ...(status().motion as object), speed_mps: 1, speed_source: 'imu' }
    const state = deriveState(inputs({ status: status({ motion }), trip: { kind: 'place', place, route } }))
    expect(state).toMatchObject({ mode: 'manual', speedMps: expect.closeTo(1, 1) })
  })

  it('does not flicker between ready and manual when speed hovers near the threshold', () => {
    const { route, place } = lRoute()
    const trip = { kind: 'place' as const, place, route }
    const at = (speed: number, previousMode?: 'ready' | 'manual') =>
      deriveState(
        inputs({
          status: status({ motion: { ...(status().motion as object), speed_mps: speed, speed_source: 'imu' } }),
          trip,
          previousMode,
        }),
      ).mode
    // Starting: needs more than 0.3 m/s to count as moving.
    expect(at(0.25, 'ready')).toBe('ready')
    expect(at(0.35, 'ready')).toBe('manual')
    // Already moving: stays manual until clearly stopped (below 0.15 m/s).
    expect(at(0.25, 'manual')).toBe('manual')
    expect(at(0.1, 'manual')).toBe('ready')
    expect(at(0, 'manual')).toBe('ready')
  })

  it('rounds speed to the 0.1 mph the speed arc shows, so IMU jitter changes nothing on screen', () => {
    const at = (speed_mps: number) =>
      chairPose(status({ motion: { ...(status().motion as object), speed_mps, speed_source: 'imu' } }), null, DEFAULT, 0)
        .speedMps
    // 0.79, 0.80 and 0.81 m/s all read 1.8 mph.
    expect(at(0.79)).toBe(at(0.81))
    expect(at(0.8)).toBeCloseTo(1.8 / 2.23694, 6)
    expect(at(0.85)).not.toBe(at(0.8))
    expect(at(0)).toBe(0)
  })

  it('rounds the heading to whole degrees in 0–359, so compass noise changes nothing on screen', () => {
    const facing = (deg: number) => chairPose(status({ heading: { deg, reference: 'true' } }), null, DEFAULT, 0).headingDeg
    expect(facing(90.3)).toBe(90)
    expect(facing(89.6)).toBe(90)
    expect(facing(359.7)).toBe(0)
  })

  it('falls back to GPS course, then the previous heading', () => {
    expect(chairPose(status({ heading: null, gps: gps({ course_deg: 200 }) }), null, DEFAULT, 0).headingDeg).toBe(200)
    expect(chairPose(status({ heading: null }), null, DEFAULT, 33).headingDeg).toBe(33)
  })

  it('prefers GPS course over a magnetic heading, but uses magnetic over none', () => {
    const magnetic = { deg: 101, reference: 'magnetic' }
    expect(chairPose(status({ heading: magnetic, gps: gps({ course_deg: 90 }) }), null, DEFAULT, 0).headingDeg).toBe(90)
    expect(chairPose(status({ heading: magnetic }), null, DEFAULT, 0).headingDeg).toBe(101)
  })

  it('never treats a stale fix as where the chair is now', () => {
    const stale = status({ gps: gps({ fresh: false, speed_mps: 1.5 }) })
    // The iPad's own location is fresher than an old fix.
    expect(chairPose(stale, IPAD, DEFAULT, 0)).toMatchObject({ position: IPAD, positionFrom: 'ipad', measured: false })
    // With nothing else, the last fix is shown, but not as measured and with no speed.
    expect(chairPose(stale, null, DEFAULT, 0)).toMatchObject({
      position: ORIGIN,
      positionFrom: 'phone-stale',
      measured: false,
      speedMps: 0,
    })
  })

  it('uses the iPad, then a default spot, before the phone has a fix', () => {
    expect(chairPose(status({ gps: null }), IPAD, DEFAULT, 0)).toMatchObject({ position: IPAD, positionFrom: 'ipad' })
    expect(chairPose(null, null, DEFAULT, 0)).toMatchObject({ position: DEFAULT, positionFrom: 'default' })
  })
})

describe('chairLink', () => {
  it('reports a healthy phone with GPS accuracy and frame rate', () => {
    expect(chairLink('online', status({ stats: { fps: 9.4 } }), 'phone')).toEqual({
      service: 'online',
      issue: null,
      phone: 'ok',
      gpsAccuracyM: 4,
      cameraFps: 9,
      positionFrom: 'phone',
      frameSource: 'phone',
      cameraConnected: false,
    })
  })

  it('follows an external camera, keeping its kind while offline', () => {
    const external = (connected: boolean) => ({ kind: 'external', connected, clock_synced: connected })
    const live = chairLink('online', status({ frame_source: external(true), stats: { fps: 19.6 } }), 'phone')
    expect(live).toMatchObject({ frameSource: 'external', cameraConnected: true, cameraFps: 20 })
    const gone = chairLink('online', status({ frame_source: external(false) }), 'phone')
    expect(gone).toMatchObject({ frameSource: 'external', cameraConnected: false, cameraFps: null })
    const offline = chairLink('offline', status({ frame_source: external(true) }), 'phone')
    expect(offline).toMatchObject({ frameSource: 'external', cameraConnected: false })
  })

  it('rounds the frame rate to whole frames a second, since the service’s changes on every status', () => {
    const fps = (value: number) => chairLink('online', status({ stats: { fps: value } }), 'phone').cameraFps
    expect([29.7, 30.1, 30.4].map(fps)).toEqual([30, 30, 30])
  })

  it('says waiting while the phone is connected but its sensors are not usable', () => {
    const link = chairLink('online', status({ sensors_ok: false, gps: gps({ fresh: false }) }), 'phone')
    expect(link.phone).toBe('waiting')
    expect(link.gpsAccuracyM).toBeNull()
  })

  it('says no phone when none is connected', () => {
    expect(chairLink('online', status({ phone_connected: false, sensors_ok: false }), 'phone')).toMatchObject({
      phone: 'none',
      cameraFps: null,
    })
  })

  it('stays offline through reconnect attempts after a loss, with the reason', () => {
    expect(chairLink('connecting', null, 'phone', true, 'too many screens are connected')).toMatchObject({
      service: 'offline',
      issue: 'too many screens are connected',
    })
    expect(chairLink('connecting', null, 'phone')).toMatchObject({ service: 'connecting', issue: null })
  })

  it('trusts nothing it last heard once the service is unreachable', () => {
    expect(chairLink('offline', status(), 'phone')).toMatchObject({ service: 'offline', phone: 'none', gpsAccuracyM: null })
  })
})

describe('progressAlong', () => {
  const { route } = lRoute()

  it('measures how far along the route the nearest point is', () => {
    expect(progressAlong(route, offset(ORIGIN, 0, 40), 0)).toBeCloseTo(40, 0)
    // 3 m off to the side of the second leg, 20 m along it.
    const beside = offset(offset(route.points[1], 90, 20), 180, 3)
    expect(progressAlong(route, beside, 0)).toBeCloseTo(120, 0)
  })

  it('clamps to the ends of the route', () => {
    expect(progressAlong(route, offset(ORIGIN, 180, 30), 0)).toBe(0)
    expect(progressAlong(route, offset(route.points[2], 90, 30), 0)).toBeCloseTo(route.lengthM, 1)
  })

  it('does not jump back to an earlier leg that passes nearby', () => {
    // A point near the corner is about equally close to both legs; having already
    // reached the second leg, progress must stay there.
    const nearCorner = offset(route.points[1], 135, 2)
    expect(progressAlong(route, nearCorner, 110)).toBeGreaterThanOrEqual(99)
  })

  it('does not jump ahead to a later leg that passes close by', () => {
    // A U: 100 m north, 5 m east, 100 m back south. Halfway up the first leg, a
    // fix drifting 3 m towards the return leg is still on the first leg.
    const up = offset(ORIGIN, 0, 100)
    const across = offset(up, 90, 5)
    const back = offset(across, 180, 100)
    const points = [ORIGIN, up, across, back]
    const u = { points, steps: [], lengthM: 205 }
    const drifted = offset(offset(ORIGIN, 0, 50), 90, 3)
    expect(progressAlong(u, drifted, 45)).toBeCloseTo(50, 0)
  })

  it('handles a route with a repeated point', () => {
    const doubled = { ...route, points: [route.points[0], route.points[0], ...route.points.slice(1)] }
    expect(progressAlong(doubled, offset(ORIGIN, 0, 10), 0)).toBeCloseTo(10, 0)
  })
})

describe('deriveState', () => {
  it('is idle and parked with no trip, with self-driving unavailable', () => {
    const state = deriveState(inputs())
    expect(state).toMatchObject({
      mode: 'idle',
      source: 'live',
      connected: true,
      batteryPct: null,
      reason: null,
      obstacles: [],
      takeOver: null,
    })
    expect(state.engageBlocked).toMatch(/not connected yet/)
  })

  it('explains why self-driving is blocked when the link is down', () => {
    expect(deriveState(inputs({ socket: 'offline' })).engageBlocked).toMatch(/driving service/)
    expect(deriveState(inputs({ status: status({ phone_connected: false }) })).engageBlocked).toMatch(/not connected/)
    expect(deriveState(inputs({ status: status({ sensors_ok: false }) })).engageBlocked).toMatch(/Waiting/)
  })

  it('counts start-up as connected and a lost socket as not', () => {
    expect(deriveState(inputs({ socket: 'connecting', status: null })).connected).toBe(true)
    expect(deriveState(inputs({ socket: 'offline' })).connected).toBe(false)
  })

  it('shows a place trip from the phone fix: progress, steps, ETA and the route ahead', () => {
    const { route, place } = lRoute()
    const at = offset(ORIGIN, 0, 30)
    const state = deriveState(
      inputs({
        trip: { kind: 'place', place, route },
        status: status({ gps: gps({ lat: at.lat, lon: at.lng }), heading: { deg: 0, reference: 'true' } }),
      }),
    )
    expect(state.mode).toBe('ready')
    expect(state.destination).toBe(place)
    expect(state.progressM).toBeCloseTo(30, 0)
    expect(state.remainingM).toBeCloseTo(route.lengthM - 30, 0)
    expect(state.etaSeconds).toBeGreaterThan(0)
    expect(state.nextStep?.step.kind).toBe('right')
    expect(state.nextStep?.distanceM).toBeCloseTo(70, 0)
    // Facing north along the first leg, the route ahead runs straight up the chair's z axis.
    expect(state.path[0]).toEqual({ x: 0, z: 0 })
    expect(state.path[1].z).toBeCloseTo(0, 1)
    expect(Math.abs(state.path[3].x)).toBeLessThan(0.5)
    expect(state.reason).toBe('Drive with the joystick')
  })

  it('calls it manual while the rider drives the trip with the joystick', () => {
    const { route, place } = lRoute()
    const state = deriveState(inputs({ trip: { kind: 'place', place, route }, status: status({ motion: null, gps: gps({ speed_mps: 1 }) }) }))
    expect(state.mode).toBe('manual')
    expect(state.reason).toBe('You’re driving')
  })

  it('arrives within the arrival radius of the destination', () => {
    const { route, place } = lRoute()
    const near = offset(place.location, 180, ARRIVE_RADIUS_M - 2)
    const state = deriveState(
      inputs({ trip: { kind: 'place', place, route }, status: status({ gps: gps({ lat: near.lat, lon: near.lng }) }) }),
    )
    expect(state.mode).toBe('arrived')
    expect(state.path).toEqual([])
  })

  it('widens the arrival radius by the GPS error, up to a limit', () => {
    const { route, place } = lRoute()
    const at = offset(place.location, 180, ARRIVE_RADIUS_M + 4)
    const fix = (accuracy_m: number) => status({ gps: gps({ lat: at.lat, lon: at.lng, accuracy_m }) })
    const trip = { kind: 'place' as const, place, route }
    expect(deriveState(inputs({ trip, status: fix(5) })).mode).toBe('arrived')
    expect(deriveState(inputs({ trip, status: fix(2) })).mode).not.toBe('arrived')
  })

  it('stays arrived when the fix wanders back outside the radius', () => {
    const { route, place } = lRoute()
    const away = offset(place.location, 180, 30)
    const state = deriveState(
      inputs({ trip: { kind: 'place', place, route }, arrived: true, status: status({ gps: gps({ lat: away.lat, lon: away.lng }) }) }),
    )
    expect(state.mode).toBe('arrived')
  })

  it('reports the connection lost until the service is heard from again', () => {
    expect(deriveState(inputs({ socket: 'connecting', status: null, linkLost: true })).connected).toBe(false)
    expect(deriveState(inputs({ socket: 'online', linkLost: false })).connected).toBe(true)
  })

  it('does not measure progress or arrival from a stale fix', () => {
    const { route, place } = lRoute()
    const atDestination = status({ gps: gps({ lat: place.location.lat, lon: place.location.lng, fresh: false }) })
    const state = deriveState(inputs({ trip: { kind: 'place', place, route }, status: atDestination, progressM: 7 }))
    expect(state.mode).not.toBe('arrived')
    expect(state.progressM).toBe(7)
  })

  it('does not measure progress from the iPad’s own location', () => {
    const { route, place } = lRoute()
    const state = deriveState(
      inputs({ trip: { kind: 'place', place, route }, status: status({ gps: null, sensors_ok: false }), ipadPosition: place.location, progressM: 12 }),
    )
    expect(state.mode).toBe('ready')
    expect(state.progressM).toBe(12)
    expect(state.reason).toBe('Waiting for the chair’s sensors')
  })

  it('holds an indoor goal with no route', () => {
    const state = deriveState(inputs({ trip: { kind: 'indoor', instruction: 'go to the kitchen' } }))
    expect(state).toMatchObject({ mode: 'ready', languageGoal: 'go to the kitchen', route: null, destination: null, path: [] })
  })
})
