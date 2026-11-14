import { useEffect, useRef, useState } from 'react'
import type { DrivingService } from '../../services/drivingService'

/** Live view from the chair's forward camera. The stream is open only while this is mounted. */
export function CameraInset({ service }: { service: DrivingService }) {
  const video = useRef<HTMLVideoElement>(null)
  const [status, setStatus] = useState<'loading' | 'live' | 'unavailable'>('loading')

  useEffect(() => {
    let cancelled = false
    void service.openCamera().then((stream) => {
      if (cancelled) return
      if (stream && video.current) {
        video.current.srcObject = stream
        setStatus('live')
      } else {
        setStatus('unavailable')
      }
    })
    return () => {
      cancelled = true
      service.closeCamera()
    }
  }, [service])

  return (
    <div className="camera-inset">
      <video ref={video} autoPlay muted playsInline />
      {status !== 'live' && <span>{status === 'loading' ? 'Connecting to camera…' : 'Camera unavailable'}</span>}
    </div>
  )
}
