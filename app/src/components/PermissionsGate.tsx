import { useEffect, useState, type ReactNode } from 'react'
import {
  locationStatus,
  microphoneStatus,
  requestLocation,
  requestMicrophone,
  type PermissionStatus,
} from '../lib/permissions'
import { Button } from './Button'
import { Dialog } from './Dialog'
import { CheckIcon, LocationIcon, MicIcon } from './icons'

const ASKED_KEY = 'fsd.permissionsAsked'

function alreadyAsked(): boolean {
  try {
    return localStorage.getItem(ASKED_KEY) === '1'
  } catch {
    return false
  }
}

/**
 * First-launch screen that asks for location and microphone access before the
 * map appears. On later launches it stays out of the way and only refreshes the
 * location if access was already granted.
 */
export function PermissionsGate() {
  const [location, setLocation] = useState<PermissionStatus | null>(null)
  const [microphone, setMicrophone] = useState<PermissionStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [asking, setAsking] = useState(false)

  useEffect(() => {
    let cancelled = false
    void Promise.all([locationStatus(), microphoneStatus()]).then(([loc, mic]) => {
      if (cancelled) return
      setLocation(loc)
      setMicrophone(mic)
      if (loc === 'granted') void requestLocation()
      // First launch: show the screen unless there is nothing left to grant or explain.
      setOpen(!alreadyAsked() && (loc !== 'granted' || mic !== 'granted'))
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (!open || !location || !microphone) return null

  const undecided = location === 'prompt' || microphone === 'prompt'

  function close() {
    try {
      localStorage.setItem(ASKED_KEY, '1')
    } catch {
      // private browsing: the screen will simply show again next launch
    }
    setOpen(false)
  }

  // One tap, two system prompts in turn. Must run from the tap for Safari to allow it.
  async function allow() {
    setAsking(true)
    if (location === 'prompt') setLocation(await requestLocation())
    if (microphone === 'prompt') setMicrophone(await requestMicrophone())
    setAsking(false)
  }

  return (
    <Dialog labelledBy="gate-title" opaque className="gate-sheet">
      <h1 id="gate-title">Before you set off</h1>
      <p className="gate-lead">The app needs two things from this iPad.</p>

      <ul className="gate-list">
        <PermissionRow
          icon={<LocationIcon size={24} />}
          title="Location"
          reason="Centres the map and finds destinations within 0.5 mi of you."
          status={location}
          fallback="Without it, search starts from a default spot."
        />
        <PermissionRow
          icon={<MicIcon size={24} />}
          title="Microphone"
          reason="Lets you say where you want to go."
          status={microphone}
          fallback="Without it, you can still type a destination."
        />
      </ul>

      {undecided ? (
        <div className="gate-actions">
          <Button variant="primary" size="wide" disabled={asking} onClick={allow}>
            {asking ? 'Waiting for your answer…' : 'Allow access'}
          </Button>
          <Button variant="quiet" disabled={asking} onClick={close}>
            Not now
          </Button>
        </div>
      ) : (
        <div className="gate-actions">
          <Button variant="primary" size="wide" onClick={close}>
            Continue
          </Button>
        </div>
      )}
    </Dialog>
  )
}

interface RowProps {
  icon: ReactNode
  title: string
  reason: string
  status: PermissionStatus
  fallback: string
}

function PermissionRow({ icon, title, reason, status, fallback }: RowProps) {
  return (
    <li className={`gate-row ${status}`}>
      <span className="gate-icon">{icon}</span>
      <span className="gate-text">
        <strong>{title}</strong>
        <span>{reason}</span>
        {status === 'denied' && (
          <span className="gate-problem">Turned off. Allow it for this site in Settings, then reload. {fallback}</span>
        )}
        {status === 'unsupported' && (
          <span className="gate-problem">Not available here. It needs a secure (HTTPS) page. {fallback}</span>
        )}
      </span>
      {status === 'granted' && (
        <span className="gate-status on">
          <CheckIcon /> Allowed
        </span>
      )}
      {status === 'denied' && <span className="gate-status">Off</span>}
      {status === 'unsupported' && <span className="gate-status">Unavailable</span>}
    </li>
  )
}
