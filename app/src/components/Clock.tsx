import { useEffect, useState } from 'react'
import { clockTime } from '../lib/format'

export function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 15000)
    return () => clearInterval(t)
  }, [])
  return <div className="clock">{clockTime(now)}</div>
}
