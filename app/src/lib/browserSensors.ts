import type { MotionLike, OrientationLike, PositionLike } from '../services/sensors/messages'
import type { SensorSource } from '../services/sensors/uplink'

// The iPad's own GPS, compass and motion, as the sensor uplink reads them.
// Same browser calls as the phone's capture page (web/capture/browser.ts).

interface PermissionRequester {
  requestPermission?: () => Promise<'granted' | 'denied' | 'default'>
}

const requester = (target: unknown): PermissionRequester['requestPermission'] =>
  (target as PermissionRequester | undefined)?.requestPermission

/** iOS asks for motion and orientation access separately, and only from a tap. */
export function motionAccessNeeded(): boolean {
  return requester(globalThis.DeviceMotionEvent) !== undefined || requester(globalThis.DeviceOrientationEvent) !== undefined
}

/**
 * Ask for motion and compass access. Call it straight from a tap handler,
 * before awaiting anything, or iOS refuses it as not user-initiated.
 */
export async function requestMotionAccess(): Promise<'granted' | 'denied' | 'not-needed'> {
  const requests = [requester(globalThis.DeviceOrientationEvent), requester(globalThis.DeviceMotionEvent)]
    .filter((request): request is NonNullable<typeof request> => request !== undefined)
    .map((request) => request.call(null).catch(() => 'denied' as const))
  if (requests.length === 0) return 'not-needed'
  const results = await Promise.all(requests)
  return results.every((result) => result === 'granted') ? 'granted' : 'denied'
}

function locationErrorMessage(error: GeolocationPositionError): string {
  switch (error.code) {
    case error.PERMISSION_DENIED:
      return 'Location access denied. Allow it in Settings › Safari › Location.'
    case error.POSITION_UNAVAILABLE:
      return 'No location fix yet.'
    case error.TIMEOUT:
      return 'Still waiting for a location fix.'
    default:
      return `Location error: ${error.message}`
  }
}

export const browserSensors: SensorSource = {
  watchPosition(onPosition, onError) {
    if (!('geolocation' in navigator)) {
      onError('This browser has no location access.')
      return () => {}
    }
    const id = navigator.geolocation.watchPosition(
      (position) => onPosition(position as PositionLike),
      (error) => onError(locationErrorMessage(error)),
      { enableHighAccuracy: true, maximumAge: 0, timeout: 15_000 },
    )
    return () => navigator.geolocation.clearWatch(id)
  },
  listenOrientation(onReading) {
    const handler = (event: Event) => onReading(event as unknown as OrientationLike)
    window.addEventListener('deviceorientation', handler)
    window.addEventListener('deviceorientationabsolute', handler)
    return () => {
      window.removeEventListener('deviceorientation', handler)
      window.removeEventListener('deviceorientationabsolute', handler)
    }
  },
  listenMotion(onMotion) {
    const handler = (event: DeviceMotionEvent) => onMotion(event as MotionLike)
    window.addEventListener('devicemotion', handler)
    return () => window.removeEventListener('devicemotion', handler)
  },
}
