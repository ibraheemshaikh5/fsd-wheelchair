import type { ChairLink, DriveMode, DriveState, LatLng, LocalPoint, Place, Route } from '../../types'
import { distanceM, toEastNorth, toLocal } from '../../lib/geo'
import { pointAt, upcomingSteps } from '../../lib/route'
import { displayFps, displayHeadingDeg, displaySpeedMps } from './displayPrecision'
import type { GpsStatus, ServiceStatus } from './status'
import type { SocketState } from './viewerSocket'

// Pure functions that turn what the driving service reports, plus the trip the
// rider chose on this iPad, into the DriveState the UI renders.
//
// The service does not drive yet: it streams the phone's sensors and holds the
// destination. So the chair's position, heading and speed are real, progress
// along the route is measured from the phone's GPS, and self-driving is never
// offered. Nothing here invents obstacles or a planned path the chair is not
// actually following.

/** Typical wheelchair cruising speed, for the arrival estimate. */
export const CRUISE_MPS = 1.1
/** Top speed shown on the speed arc, about 4 mph. */
export const MAX_MPS = 1.8
/** Moving faster than this with a trip set means the rider is driving it with the joystick. */
export const MOVING_MPS = 0.3
/** Below this a chair already moving counts as stopped again (hysteresis with MOVING_MPS). */
export const STOPPED_MPS = 0.15
/** Within this distance of the destination (plus the GPS error) counts as arrived. */
export const ARRIVE_RADIUS_M = 8
/** A new GPS fix may move progress back by at most this much (the chair rarely reverses far). */
const MAX_BACKTRACK_M = 15
/** Progress may move ahead this far per fix freely; beyond it, a later leg must be clearly closer. */
const MAX_AHEAD_M = 15
/** Per metre beyond MAX_AHEAD_M, how much closer a point further ahead must be to win. */
const AHEAD_WEIGHT = 0.1
/** GPS error added to the arrival radius, at most this much. */
const MAX_ARRIVE_SLACK_M = 10
/** How far of the route ahead to draw in the chair view. */
const LOOKAHEAD_M = 30

export type Trip =
  | { kind: 'place'; place: Place; route: Route }
  | { kind: 'indoor'; instruction: string }

export interface LiveInputs {
  socket: SocketState
  status: ServiceStatus | null
  /** The iPad's own location, used for the map until the phone has a fix. */
  ipadPosition: LatLng | null
  defaultPosition: LatLng
  trip: Trip | null
  /** Metres along the route at the last update; progress is kept monotonic-ish from here. */
  progressM: number
  /** Heading at the last update, kept when no new heading is known. */
  previousHeadingDeg: number
  /** Whether this trip already arrived; arrival is kept until the trip changes. */
  arrived: boolean
  /** True from losing the service until it sends a usable status again. */
  linkLost: boolean
  /** Why the service was last lost, while it is still lost. */
  linkIssue: string | null
  /** The mode at the last update, so ready/manual has hysteresis. */
  previousMode?: DriveMode
}

interface Pose {
  position: LatLng
  positionFrom: ChairLink['positionFrom']
  /** True only for a fresh phone fix: the one thing that says where the chair is now. */
  measured: boolean
  /** GPS error of a measured fix, in metres. */
  accuracyM: number | null
  headingDeg: number
  speedMps: number
}

export function chairPose(
  status: ServiceStatus | null,
  ipadPosition: LatLng | null,
  defaultPosition: LatLng,
  previousHeadingDeg: number,
): Pose {
  const gps = status?.gps ?? null
  const freshGps = gps?.fresh ? gps : null
  // Freshest first. The service keeps the last fix forever (even from an earlier
  // session), so a stale fix only beats having nothing at all.
  const fromGps = (fix: NonNullable<typeof gps>): LatLng => ({ lat: fix.lat, lng: fix.lon })
  const [position, positionFrom]: [LatLng, Pose['positionFrom']] = freshGps
    ? [fromGps(freshGps), 'phone']
    : ipadPosition
      ? [ipadPosition, 'ipad']
      : gps
        ? [fromGps(gps), 'phone-stale']
        : [defaultPosition, 'default']
  // True north lines up with the map; a magnetic heading is about 11° off around
  // College Park, still better than none.
  const heading = status?.heading ?? null
  const trueHeading = heading?.reference === 'true' ? heading.deg : null
  const headingDeg = trueHeading ?? freshGps?.course_deg ?? heading?.deg ?? previousHeadingDeg
  return {
    position,
    positionFrom,
    measured: freshGps !== null,
    accuracyM: freshGps?.accuracy_m ?? null,
    headingDeg: displayHeadingDeg(headingDeg),
    speedMps: displaySpeedMps(chairSpeed(status, freshGps)),
  }
}

/**
 * The board's estimate from the stick it sends first (instant, and works indoors);
 * else fresh IMU-derived motion; else the fresh GPS speed, which lags; else stopped.
 */
function chairSpeed(status: ServiceStatus | null, freshGps: GpsStatus | null): number {
  const chair = status?.chair ?? null
  if (chair?.fresh) return Math.abs(chair.speed_mps)
  const motion = status?.motion ?? null
  const motionSpeed = motion?.fresh ? motion.speed_mps : null
  return Math.max(0, motionSpeed ?? freshGps?.speed_mps ?? 0)
}

export function chairLink(
  socket: SocketState,
  status: ServiceStatus | null,
  positionFrom: Pose['positionFrom'],
  linkLost = false,
  linkIssue: string | null = null,
): ChairLink {
  // Once the iPad loses the service, nothing it last heard about the phone is current.
  const live = socket === 'online' ? status : null
  const phone: ChairLink['phone'] = !live?.phone_connected ? 'none' : live.sensors_ok ? 'ok' : 'waiting'
  // The kind is configuration, so it is kept from the last status even while offline.
  const frameSource = status?.frame_source?.kind ?? 'phone'
  const cameraConnected = live?.frame_source?.kind === 'external' && live.frame_source.connected
  return {
    // After a loss, retrying (even a socket that opened but has sent nothing yet)
    // is still offline as far as the rider is concerned.
    service: linkLost ? 'offline' : socket,
    issue: linkLost ? linkIssue : null,
    phone,
    gpsAccuracyM: live?.gps?.fresh ? live.gps.accuracy_m : null,
    cameraFps: frameSource === 'external' ? (cameraConnected ? displayFps(live?.stats.fps ?? 0) : null)
      : live?.phone_connected ? displayFps(live.stats.fps) : null,
    positionFrom,
    frameSource,
    cameraConnected,
  }
}

/** Metres along `route` of the point nearest `position`, preferring not to jump far back from `previousM`. */
export function progressAlong(route: Route, position: LatLng, previousM: number): number {
  const pts = route.points
  let best = { score: Infinity, along: previousM }
  let walked = 0
  for (let i = 1; i < pts.length; i++) {
    const a = toEastNorth(position, pts[i - 1])
    const b = toEastNorth(position, pts[i])
    const dx = b.e - a.e
    const dy = b.n - a.n
    const len = Math.hypot(dx, dy)
    // Projection of the chair (the origin here) onto segment a→b.
    const t = len > 0 ? Math.max(0, Math.min(1, -(a.e * dx + a.n * dy) / (len * len))) : 0
    const off = Math.hypot(a.e + dx * t, a.n + dy * t)
    const along = walked + len * t
    // Points well behind the last progress only win if they are much closer, and
    // points far ahead (a later leg passing nearby) only if they are clearly closer.
    const backtrack = Math.max(0, previousM - MAX_BACKTRACK_M - along)
    const ahead = AHEAD_WEIGHT * Math.max(0, along - previousM - MAX_AHEAD_M)
    const score = off + backtrack + ahead
    if (score < best.score) best = { score, along }
    walked += len
  }
  return Math.min(route.lengthM, Math.max(0, best.along))
}

function routeAhead(route: Route, progressM: number, pose: Pose): LocalPoint[] {
  // Start at the chair: when it is off the route, the strip shows the way back onto it.
  const path: LocalPoint[] = [{ x: 0, z: 0 }]
  const end = Math.min(route.lengthM, progressM + LOOKAHEAD_M)
  for (let d = progressM; d <= end; d += 1.5) {
    path.push(toLocal(pose.position, pose.headingDeg, pointAt(route, d).position))
  }
  return path
}

function engageBlocked(link: ChairLink): string {
  if (link.service !== 'online') return 'Not connected to the driving service'
  if (link.phone === 'none') return 'The phone on the chair is not connected'
  if (link.phone === 'waiting') return 'Waiting for the phone’s camera and GPS'
  return 'Self-driving is not connected yet. Drive with the joystick.'
}

/**
 * Manual once speed passes MOVING_MPS, and only back to ready below STOPPED_MPS.
 * Fused speed updates ~30 times a second, so a single threshold would flicker.
 */
function modeFor(
  trip: Trip | null,
  arrived: boolean,
  speedMps: number,
  previousMode?: DriveMode,
  selfDriving = false,
): DriveMode {
  if (selfDriving) return 'driving'
  if (!trip) return 'idle'
  if (arrived) return 'arrived'
  const threshold = previousMode === 'manual' ? STOPPED_MPS : MOVING_MPS
  return speedMps > threshold ? 'manual' : 'ready'
}

function reasonFor(mode: DriveMode, link: ChairLink): string | null {
  if (mode === 'idle') return null
  if (mode === 'arrived') return 'Arrived'
  if (mode === 'driving') return 'Self-driving'
  if (link.phone !== 'ok') return 'Waiting for the chair’s sensors'
  // Nothing on the iPad controls the chair yet, so never claim it is holding.
  return mode === 'manual' ? 'You’re driving' : 'Drive with the joystick'
}

type TripFields = Pick<
  DriveState,
  | 'destination'
  | 'route'
  | 'languageGoal'
  | 'progressM'
  | 'remainingM'
  | 'etaSeconds'
  | 'nextStep'
  | 'followingStep'
  | 'path'
>

const NO_TRIP: TripFields = {
  destination: null,
  route: null,
  languageGoal: null,
  progressM: 0,
  remainingM: 0,
  etaSeconds: 0,
  nextStep: null,
  followingStep: null,
  path: [],
}

function tripFields(
  trip: Trip | null,
  pose: Pose,
  previousM: number,
  previouslyArrived: boolean,
): TripFields & { arrived: boolean } {
  if (!trip) return { ...NO_TRIP, arrived: false }
  if (trip.kind === 'indoor') return { ...NO_TRIP, languageGoal: trip.instruction, arrived: false }
  const { route, place } = trip
  // Only a fresh phone fix says where the chair is; the iPad's location or an old fix does not count.
  const { measured } = pose
  const progressM = measured ? progressAlong(route, pose.position, previousM) : previousM
  const remainingM = Math.max(0, route.lengthM - progressM)
  const { next, following } = upcomingSteps(route, progressM)
  // Once arrived, stay arrived: GPS wander back outside the radius must not undo it.
  const slackM = Math.min(pose.accuracyM ?? 0, MAX_ARRIVE_SLACK_M)
  const arrived =
    previouslyArrived || (measured && distanceM(pose.position, place.location) <= ARRIVE_RADIUS_M + slackM)
  return {
    destination: place,
    route,
    languageGoal: null,
    progressM,
    remainingM,
    etaSeconds: remainingM / CRUISE_MPS,
    nextStep: next,
    followingStep: following,
    path: arrived ? [] : routeAhead(route, progressM, pose),
    arrived,
  }
}

/** The whole DriveState for the inputs. */
export function deriveState(input: LiveInputs): DriveState {
  const pose = chairPose(input.status, input.ipadPosition, input.defaultPosition, input.previousHeadingDeg)
  const link = chairLink(input.socket, input.status, pose.positionFrom, input.linkLost, input.linkIssue)
  const { arrived, ...trip } = tripFields(input.trip, pose, input.progressM, input.arrived)
  const chair = input.status?.chair?.fresh ? input.status.chair : null
  const mode = modeFor(input.trip, arrived, pose.speedMps, input.previousMode, chair?.driver === 'fsd')
  return {
    mode,
    source: 'live',
    // Still connecting at start-up is not a lost connection; once lost, it stays
    // lost through every reconnect attempt until the service sends a status again.
    connected: link.service !== 'offline',
    link,
    engageBlocked: engageBlocked(link),
    position: pose.position,
    headingDeg: pose.headingDeg,
    speedMps: pose.speedMps,
    maxSpeedMps: MAX_MPS,
    // The chair's battery is not reported yet.
    batteryPct: null,
    ...trip,
    pathStatus: 'clear',
    obstacles: [],
    yieldingToId: null,
    reason: reasonFor(mode, link),
    takeOver: chair?.takeover
      ? 'You took over with the joystick'
      : chair?.dropout_latched
        ? 'Connection to the chair dropped; self-driving is off'
        : null,
  }
}
