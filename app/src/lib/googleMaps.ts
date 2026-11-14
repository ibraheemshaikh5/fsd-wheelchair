const KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined

export const hasGoogleKey = Boolean(KEY)

type Listener = () => void
const failureListeners = new Set<Listener>()
let failed = false
let loading: Promise<void> | null = null

function fail() {
  failed = true
  failureListeners.forEach((fn) => fn())
}

/** True once the Maps SDK is loaded and the key has not been rejected. */
export function googleReady(): boolean {
  return !failed && typeof google !== 'undefined' && Boolean(google.maps?.Map)
}

/** Fires if the script fails to load or Google rejects the key. */
export function onGoogleFailure(fn: Listener): () => void {
  failureListeners.add(fn)
  if (failed) fn()
  return () => failureListeners.delete(fn)
}

export function loadGoogleMaps(): Promise<void> {
  if (!KEY) return Promise.reject(new Error('VITE_GOOGLE_MAPS_API_KEY is not set'))
  loading ??= new Promise<void>((resolve, reject) => {
    const w = window as unknown as Record<string, unknown>
    w.__fsdMapsReady = () => resolve()
    w.gm_authFailure = fail
    const script = document.createElement('script')
    script.src =
      `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(KEY)}` +
      '&v=weekly&loading=async&libraries=places,marker,routes&callback=__fsdMapsReady'
    script.async = true
    script.onerror = () => {
      fail()
      reject(new Error('Google Maps failed to load'))
    }
    document.head.append(script)
  })
  return loading
}

/** Loads the SDK if a key is configured; resolves to whether Google is usable. */
export async function ensureGoogle(): Promise<boolean> {
  if (!hasGoogleKey || failed) return false
  try {
    await loadGoogleMaps()
  } catch {
    return false
  }
  return googleReady()
}
