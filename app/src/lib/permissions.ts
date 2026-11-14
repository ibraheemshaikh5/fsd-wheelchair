import { publishDeviceLocation } from './deviceLocation'

export type PermissionStatus = 'prompt' | 'granted' | 'denied' | 'unsupported'

// ----- status without prompting -----

async function query(name: string): Promise<PermissionStatus | null> {
  try {
    return (await navigator.permissions.query({ name: name as PermissionName })).state
  } catch {
    return null // browser can't report this permission
  }
}

export async function locationStatus(): Promise<PermissionStatus> {
  if (!window.isSecureContext || !('geolocation' in navigator)) return 'unsupported'
  return (await query('geolocation')) ?? 'prompt'
}

export async function microphoneStatus(): Promise<PermissionStatus> {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) return 'unsupported'
  return (await query('microphone')) ?? 'prompt'
}

// ----- requests; these show the system prompt if the user hasn't decided -----

export function requestLocation(): Promise<PermissionStatus> {
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        publishDeviceLocation({ lat: pos.coords.latitude, lng: pos.coords.longitude })
        resolve('granted')
      },
      // Anything other than a refusal means access was allowed but no fix came back.
      (err) => resolve(err.code === err.PERMISSION_DENIED ? 'denied' : 'granted'),
      { timeout: 10000, maximumAge: 60000 },
    )
  })
}

export async function requestMicrophone(): Promise<PermissionStatus> {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    stream.getTracks().forEach((t) => t.stop())
    return 'granted'
  } catch (e) {
    return (e as DOMException).name === 'NotFoundError' ? 'unsupported' : 'denied'
  }
}
