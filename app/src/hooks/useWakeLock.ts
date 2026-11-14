import { useEffect } from 'react'

// Typed locally so the hook does not depend on which lib.dom version is installed.
interface ScreenLock {
  readonly released: boolean
  release(): Promise<void>
}

interface WakeLockApi {
  request(type: 'screen'): Promise<ScreenLock>
}

function wakeLockApi(): WakeLockApi | undefined {
  return (navigator as Navigator & { wakeLock?: WakeLockApi }).wakeLock
}

/**
 * Keeps the iPad screen on while `active`, so it cannot auto-lock mid-trip.
 * Safari drops the lock whenever the page is hidden, so it is taken again each
 * time the page becomes visible. Where the API is missing this does nothing.
 */
export function useWakeLock(active: boolean): void {
  useEffect(() => {
    const api = wakeLockApi()
    if (!active || !api) return
    let lock: ScreenLock | null = null
    let disposed = false

    const release = (held: ScreenLock | null) => {
      held?.release().catch((err: unknown) => console.warn('Screen wake lock release failed', err))
    }

    const acquire = async () => {
      // Still held (the page never really hid): asking again would stack a second lock.
      if (document.visibilityState !== 'visible' || (lock && !lock.released)) return
      try {
        const next = await api.request('screen')
        if (disposed) {
          release(next)
          return
        }
        // Two quick visibility changes can race; keep only the newest lock.
        release(lock)
        lock = next
      } catch (err) {
        // Low Power Mode can refuse it; the screen then sleeps as it normally would.
        console.warn('Screen wake lock unavailable', err)
      }
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void acquire()
    }

    void acquire()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      disposed = true
      document.removeEventListener('visibilitychange', onVisibility)
      release(lock)
      lock = null
    }
  }, [active])
}
