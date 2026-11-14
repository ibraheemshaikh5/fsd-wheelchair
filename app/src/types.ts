// Shared shapes between the iPad app and the driving service.
// Units are SI (metres, metres/second, degrees clockwise from north); the UI converts for display.

export interface LatLng {
  lat: number
  lng: number
}

export interface Place {
  id: string
  name: string
  address?: string
  location: LatLng
  /** Straight-line distance from the chair when the search ran. */
  distanceM: number
}

export type ManeuverKind =
  | 'straight'
  | 'left'
  | 'right'
  | 'slight-left'
  | 'slight-right'
  | 'u-turn'
  | 'arrive'

export interface RouteStep {
  kind: ManeuverKind
  /** Street or short instruction shown on the turn card. */
  label: string
  /** Distance along the route at which this manoeuvre happens. */
  offsetM: number
}

export interface Route {
  points: LatLng[]
  steps: RouteStep[]
  lengthM: number
}

/** A point in the chair's frame: x metres to the right, z metres ahead. */
export interface LocalPoint {
  x: number
  z: number
}

export interface Obstacle {
  id: string
  kind: 'pedestrian' | 'vehicle' | 'object'
  x: number
  z: number
}

/**
 * idle     no destination or goal
 * ready    destination (with route) or indoor goal set, chair holding
 * driving  self-driving engaged
 * arrived  reached the destination, chair holding
 * manual   the rider is driving with the joystick; a destination may still be set
 */
export type DriveMode = 'idle' | 'ready' | 'driving' | 'arrived' | 'manual'

/** Whether the space the chair is about to move into is free. */
export type PathStatus = 'clear' | 'slowing' | 'blocked'

export interface DriveState {
  mode: DriveMode
  connected: boolean
  position: LatLng
  headingDeg: number
  speedMps: number
  maxSpeedMps: number
  batteryPct: number
  /** Outdoor trip: a map destination with a GPS route. */
  destination: Place | null
  route: Route | null
  /**
   * Indoor trip: a spoken instruction ("go to the kitchen") the model follows from
   * the camera. There is no GPS indoors, so route, progress, ETA and steps are empty.
   */
  languageGoal: string | null
  progressM: number
  remainingM: number
  etaSeconds: number
  nextStep: { step: RouteStep; distanceM: number } | null
  followingStep: { step: RouteStep; distanceM: number } | null
  /** Planned trajectory ahead of the chair, in the chair's frame. */
  path: LocalPoint[]
  pathStatus: PathStatus
  obstacles: Obstacle[]
  /** The obstacle the chair is currently slowing or waiting for, if any. */
  yieldingToId: string | null
  /** One short line on what the chair is doing and why, e.g. "Waiting for pedestrian". */
  reason: string | null
  /**
   * Set when the chair needs the rider to take the joystick, with the reason.
   * The chair is stopped while this is set.
   */
  takeOver: string | null
}
