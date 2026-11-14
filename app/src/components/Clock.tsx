import { clockTime } from '../lib/format'
import { useNow } from '../hooks/useNow'

export function Clock() {
  const now = useNow()
  return <div className="clock">{clockTime(new Date(now))}</div>
}
