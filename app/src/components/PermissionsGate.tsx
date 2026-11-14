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
/** Set once the rider tapped Allow, so later launches may ask Safari for location again. */
const LOCATION_ALLOWED_KEY = 'fsd.locationAllowed'

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

function writeFlag(key: string): void {
  try {
    localStorage.setItem(key, '1')
  } catch {
    // private browsing: the flag is simply not remembered
  }
}

/**
 * First-launch screen that asks for location and microphone access before the
 * map appears. On later launches it stays out of the way and only refreshes the
 * location, which Safari may need to ask for again: it often reports 'prompt'
 * even after access was allowed on an earlier visit.
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
      const firstLaunch = !readFlag(ASKED_KEY)
      // Safari's location prompt needs no tap, so ask straight away when the rider
      // allowed it before (Safari often forgets). Never after "Not now", and never on
      // first launch, where the screen below explains why before the system prompt.
      if (loc === 'granted' || (loc === 'prompt' && readFlag(LOCATION_ALLOWED_KEY))) {
        void requestLocation().then((status) => {
          if (!cancelled) setLocation(status)
        })
      }
      // First launch: show the screen unless there is nothing left to grant or explain.
      setOpen(firstLaunch && (loc !== 'granted' || mic !== 'granted'))
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (!open || !location || !microphone) return null

  const undecided = location === 'prompt' || microphone === 'prompt'

  function close() {
    // In private browsing this is not remembered and the screen shows again next launch.
    writeFlag(ASKED_KEY)
    setOpen(false)
  }

  // One tap, two system prompts in turn. Must run from the tap for Safari to allow it.
  async function allow() {
    setAsking(true)
    writeFlag(LOCATION_ALLOWED_KEY)
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

      <div className="gate-actions">
        {undecided ? (
          <>
            <Button variant="primary" size="wide" disabled={asking} onClick={allow}>
              {asking ? 'Waiting for your answer…' : 'Allow access'}
            </Button>
            <Button variant="quiet" disabled={asking} onClick={close}>
              Not now
            </Button>
          </>
        ) : (
          <Button variant="primary" size="wide" onClick={close}>
            Continue
          </Button>
        )}
      </div>
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
