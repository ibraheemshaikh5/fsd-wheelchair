import { parseStatus, type ServiceStatus } from './status'

// The iPad's connection to the driving service: /ws/viewer, which sends a status
// as JSON text about thirty times a second, each followed by the phone's latest
// camera frame as JPEG bytes when there is a new one (up to the phone's frame
// rate). Reconnects with backoff on its own.

const BACKOFF_BASE_MS = 500
const BACKOFF_MAX_MS = 5000
/**
 * The service sends a status about every 33 ms. Hearing nothing for this long means
 * the link is dead even if the socket has not noticed yet (Wi-Fi dropped, iPad
 * slept), so it is closed and reported lost straight away.
 */
export const SILENCE_TIMEOUT_MS = 2000
const CLOSE_TOO_MANY_VIEWERS = 4429

export type SocketState = 'connecting' | 'online' | 'offline'

/** The parts of a browser WebSocket used here, so tests can pass a fake. */
export interface ViewerSocket {
  binaryType: BinaryType
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onclose: ((event: CloseEvent) => void) | null
  close(): void
}

export interface Timers {
  set(fn: () => void, ms: number): number
  clear(id: number): void
}

export interface ViewerOptions {
  url: string
  createSocket: (url: string) => ViewerSocket
  timers: Timers
  onState: (state: SocketState, reason: string | null) => void
  onStatus: (status: ServiceStatus) => void
  onFrame: (jpeg: ArrayBuffer) => void
}

export interface ViewerConnection {
  /** Closes the socket for good. */
  close(): void
}

/** ws:// or wss:// to /ws/viewer on the page's own host (the dev server proxies it to the service). */
export function viewerSocketUrl(location: Pick<Location, 'protocol' | 'host'>): string {
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/viewer`
}

export function backoffMs(attempt: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt)
}

export function connectViewer(options: ViewerOptions): ViewerConnection {
  const { timers } = options
  let attempt = 0
  let socket: ViewerSocket | null = null
  let retryTimer: number | null = null
  let silenceTimer: number | null = null
  let stopped = false

  const clearSilence = () => {
    if (silenceTimer !== null) timers.clear(silenceTimer)
    silenceTimer = null
  }

  const lose = (current: ViewerSocket, reason: string) => {
    if (stopped || socket !== current) return
    socket = null
    clearSilence()
    current.onopen = current.onmessage = current.onclose = null
    current.close()
    options.onState('offline', reason)
    retryTimer = timers.set(open, backoffMs(attempt))
    attempt += 1
  }

  const watchSilence = (current: ViewerSocket) => {
    clearSilence()
    silenceTimer = timers.set(() => lose(current, 'no usable status from the service'), SILENCE_TIMEOUT_MS)
  }

  function open() {
    retryTimer = null
    const current = options.createSocket(options.url)
    socket = current
    current.binaryType = 'arraybuffer'
    options.onState('connecting', null)
    watchSilence(current)
    current.onopen = () => options.onState('online', null)
    current.onmessage = (event) => {
      // Any message proves the service kept this socket: it may accept one and
      // close it straight away when it has too many viewers.
      attempt = 0
      if (event.data instanceof ArrayBuffer) {
        options.onFrame(event.data)
        return
      }
      if (typeof event.data !== 'string') return
      const status = parseStatus(event.data)
      if (!status) return
      // Only a usable status counts as hearing from the service: if every status
      // were rejected (a newer service shape), the watchdog must still fire.
      watchSilence(current)
      options.onStatus(status)
    }
    current.onclose = (event) =>
      lose(current, event.code === CLOSE_TOO_MANY_VIEWERS ? 'too many screens are connected' : 'connection lost')
  }

  open()
  return {
    close() {
      stopped = true
      clearSilence()
      if (retryTimer !== null) timers.clear(retryTimer)
      retryTimer = null
      socket?.close()
      socket = null
    },
  }
}
