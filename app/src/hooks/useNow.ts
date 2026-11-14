import { useEffect, useState } from 'react'

export const MINUTE_MS = 60_000

/**
 * The current time in epoch milliseconds, re-read on every `periodMs` boundary
 * (each whole minute by default). Clocks and arrival times show minutes, so
 * ticking on the boundary flips them exactly when the minute changes instead
 * of up to a whole period late.
 */
export function useNow(periodMs: number = MINUTE_MS): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined
    // A fresh timeout each period re-aligns to the boundary, which a plain
    // setInterval would not after Safari throttles a backgrounded tab.
    const schedule = () => {
      timer = setTimeout(() => {
        setNow(Date.now())
        schedule()
      }, periodMs - (Date.now() % periodMs))
    }
    // Waking the iPad shows the page again before the next boundary.
    const onVisible = () => {
      if (document.visibilityState === 'visible') setNow(Date.now())
    }
    schedule()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [periodMs])

  return now
}
