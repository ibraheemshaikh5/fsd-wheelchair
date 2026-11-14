import type { SensorUplinkView } from '../hooks/useSensorUplink'
import { Button } from './Button'

/**
 * Asks for what the iPad needs to stand in for the chair's phone: a tap to allow
 * motion and compass (iOS will not ask without one), or to take the sensor
 * stream back after another device took it over.
 */
export function SensorUplinkNotice({ uplink }: { uplink: SensorUplinkView }) {
  if (uplink.state === 'taken-over') {
    return (
      <div className="uplink-notice" role="status">
        <span>This iPad stopped sending its location: {uplink.detail}.</span>
        <Button variant="primary" onClick={uplink.allowMotion}>
          Send from this iPad
        </Button>
      </div>
    )
  }
  if (!uplink.needsMotionAccess) return null
  return (
    <div className="uplink-notice" role="status">
      <span>The chair uses this iPad’s compass and motion sensors.</span>
      <Button variant="primary" onClick={uplink.allowMotion}>
        Allow motion
      </Button>
    </div>
  )
}
