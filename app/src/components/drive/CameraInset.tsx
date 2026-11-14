import { useEffect, useRef, useState } from 'react'
import type { CameraFrame, DrivingService } from '../../services/drivingService'

type Status = 'loading' | 'live' | 'stale' | 'unavailable'

const STATUS_TEXT: Record<Exclude<Status, 'live'>, string> = {
  loading: 'Connecting to camera…',
  stale: 'No live picture',
  unavailable: 'Camera unavailable',
}

interface Props {
  service: DrivingService
}

/**
 * Live view from the chair's forward camera, open only while this is mounted.
 * A stream (simulator) plays in a video element; the phone's JPEG frames swap
 * into an image. Tap to switch between the inset and a large view.
 */
export function CameraInset({ service }: Props) {
  const video = useRef<HTMLVideoElement>(null)
  const [status, setStatus] = useState<Status>('loading')
  const [frame, setFrame] = useState<CameraFrame | null>(null)
  const [large, setLarge] = useState(false)

  useEffect(() => {
    let cancelled = false
    let unsubscribe: (() => void) | null = null
    void service.openCamera().then((feed) => {
      if (cancelled) return
      if (!feed) {
        setStatus('unavailable')
      } else if (feed.kind === 'stream') {
        if (video.current) video.current.srcObject = feed.stream
        setStatus('live')
      } else {
        unsubscribe = feed.subscribe((next) => {
          setFrame(next)
          // No picture yet but one is on its way: still connecting.
          setStatus(next.fresh ? (next.url ? 'live' : 'loading') : 'stale')
        })
      }
    })
    return () => {
      cancelled = true
      unsubscribe?.()
      service.closeCamera()
    }
  }, [service])

  return (
    <button
      type="button"
      className={`camera-inset ${large ? 'large' : ''} ${status}`}
      aria-label={large ? 'Shrink camera view' : 'Enlarge camera view'}
      onClick={() => setLarge((on) => !on)}
    >
      {frame ? (
        frame.url && <img src={frame.url} alt="Chair camera" />
      ) : (
        <video ref={video} autoPlay muted playsInline />
      )}
      {status !== 'live' && <span className="camera-note">{STATUS_TEXT[status]}</span>}
    </button>
  )
}
