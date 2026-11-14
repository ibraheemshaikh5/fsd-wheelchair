import { useCallback, useEffect, useState } from 'react'
import { browserSensors, motionAccessNeeded, requestMotionAccess } from '../lib/browserSensors'
import type { MotionLike } from '../services/sensors/messages'
import { connectSensorUplink, phoneSocketUrl, type SensorSource, type UplinkState } from '../services/sensors/uplink'

/** No motion event this long after starting means iOS has not granted access yet. */
const MOTION_GRACE_MS = 1500

const timers = {
  set: (fn: () => void, ms: number) => window.setTimeout(fn, ms),
  clear: (id: number) => window.clearTimeout(id),
}

export interface SensorUplinkView {
  state: UplinkState
  detail: string | null
  /** True when motion and compass readings need a tap to allow them (iOS). */
  needsMotionAccess: boolean
  allowMotion: () => void
}

function useVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible')
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return visible
}

/**
 * Sends this iPad's GPS, compass and motion to the driving service while
 * `enabled` (the service takes frames from an external camera) and the page is
 * visible. A hidden page stops, like the phone's, and resumes when shown again.
 */
export function useSensorUplink(enabled: boolean): SensorUplinkView {
  const visible = useVisible()
  const [state, setState] = useState<UplinkState>('stopped')
  const [detail, setDetail] = useState<string | null>(null)
  const [sawMotion, setSawMotion] = useState(false)
  const [motionChecked, setMotionChecked] = useState(false)
  // Bumped by a tap on Allow, so a stream another device took over can resume.
  const [restart, setRestart] = useState(0)
  const active = enabled && visible

  useEffect(() => {
    if (!active) return
    const sensors: SensorSource = {
      ...browserSensors,
      listenMotion: (onMotion: (event: MotionLike) => void) =>
        browserSensors.listenMotion((event) => {
          setSawMotion(true)
          onMotion(event)
        }),
    }
    const grace = window.setTimeout(() => setMotionChecked(true), MOTION_GRACE_MS)
    const uplink = connectSensorUplink({
      url: phoneSocketUrl(window.location),
      userAgent: navigator.userAgent,
      createSocket: (url) => new WebSocket(url),
      sensors,
      timers,
      now: () => Date.now(),
      onState: (next, why) => {
        setState(next)
        setDetail(why)
      },
    })
    return () => {
      window.clearTimeout(grace)
      uplink.close()
    }
  }, [active, restart])

  const allowMotion = useCallback(() => {
    // Straight from the tap, before any await, or iOS refuses.
    void requestMotionAccess().then(() => setRestart((n) => n + 1))
  }, [])

  return {
    state: active ? state : 'stopped',
    detail: active ? detail : null,
    needsMotionAccess: active && motionChecked && !sawMotion && motionAccessNeeded(),
    allowMotion,
  }
}
