import type { DriveState, Place } from '../types'
import { MockDrivingService } from './mockDrivingService'

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
   * The chair's forward camera as a live stream, or null if there is none.
   * Called when the rider opens the camera inset.
   */
  openCamera(): Promise<MediaStream | null>
  /** Called when the rider closes the camera inset. */
  closeCamera(): void
}

export function createDrivingService(): DrivingService {
  return new MockDrivingService()
}
