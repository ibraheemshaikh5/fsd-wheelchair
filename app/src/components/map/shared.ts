import type { DriveState, Place } from '../../types'

export interface MapProps {
  state: DriveState
  /** Search results to pin while a destination is being chosen. */
  results: Place[]
  onSelect: (place: Place) => void
  /** Satellite imagery instead of the grey-and-white street map. */
  satellite: boolean
}

/**
 * How far in the map sits around the chair before anything is searched, as a
 * Google zoom level. A whole number, so Google's street labels are drawn at
 * their native size and stay sharp.
 */
export const HOME_ZOOM = 18
/** Zoom while following the chair on a drive. */
export const FOLLOW_ZOOM = 19

export const ROUTE_BLUE = '#3e6ae1'
export const CHAIR_RED = '#e03a3a'
