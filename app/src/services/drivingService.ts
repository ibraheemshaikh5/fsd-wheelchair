import type { DriveState, Place } from '../types'
import { LiveDrivingService } from './live/liveDrivingService'
import { MockDrivingService } from './mockDrivingService'

/** A frame from the chair's camera, as an object URL the service owns and revokes. */
export interface CameraFrame {
  /** Null until the first frame arrives. */
  url: string | null
  /** False once the frame is too old to show the chair's surroundings. */
  fresh: boolean
  /** Frames are still arriving, but were captured long enough ago that the picture lags behind. */
  delayed: boolean
}

/** The forward camera: a live stream (simulator) or a sequence of JPEG frames (the phone). */
export type CameraFeed =
  | { kind: 'stream'; stream: MediaStream }
  | { kind: 'frames'; subscribe(listener: (frame: CameraFrame) => void): () => void }

/**
 * Everything the iPad needs from the driving service. The service owns all
 * decisions; the app only renders DriveState and sends the commands below.
 *
 * To connect the real backend, implement this interface over whatever transport
 * it exposes (WebSocket, WebRTC data channel, ...) and return it from
 * createDrivingService(). Nothing else in the app needs to change.
 */
export interface DrivingService {
  getState(): DriveState
  /** Calls `listener` after every state change. Returns an unsubscribe function. */
  subscribe(listener: () => void): () => void
  /** Plans a route to `place` and moves to `ready`. Rejects if no route is found. */
  setDestination(place: Place): Promise<void>
  /**
   * Sets an indoor goal from a spoken instruction and moves to `ready`.
   * Replaces any map destination.
   */
  setLanguageGoal(instruction: string): Promise<void>
  /** Ends the trip and returns to `idle`. Stops the chair if it is driving. */
  clearDestination(): void
  /** Starts or resumes self-driving towards the current destination or goal. */
  engage(): void
  /** Stops the chair and holds. The destination or goal is kept. */
  disengage(): void
  /**
   * The chair's forward camera, or null if there is none.
   * Called when the rider opens the camera inset.
   */
  openCamera(): Promise<CameraFeed | null>
  /** Called when the rider closes the camera inset. */
  closeCamera(): void
  /** Stops all background work (sockets, timers, listeners). Optional. */
  dispose?(): void
}

/**
 * The live driving service by default. `?sim` in the address, or
 * VITE_DRIVING_SERVICE=sim, runs the in-browser simulator instead.
 */
export function createDrivingService(): DrivingService {
  const simulate =
    new URLSearchParams(window.location.search).has('sim') || import.meta.env.VITE_DRIVING_SERVICE === 'sim'
  return simulate ? new MockDrivingService() : new LiveDrivingService()
}
