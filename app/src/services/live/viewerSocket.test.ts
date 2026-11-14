import { describe, expect, it } from 'vitest'
import type { ServiceStatus } from './status'
import { FakeSocket, ManualTimers, statusMessage } from './testing'
import { SILENCE_TIMEOUT_MS, backoffMs, connectViewer, type SocketState, viewerSocketUrl } from './viewerSocket'

function setup() {
  const sockets: FakeSocket[] = []
  const timers = new ManualTimers()
  const states: [SocketState, string | null][] = []
  const statuses: ServiceStatus[] = []
  const frames: ArrayBuffer[] = []
  const connection = connectViewer({
    url: 'ws://ipad/ws/viewer',
    createSocket: (url) => {
      const socket = new FakeSocket(url)
      sockets.push(socket)
      return socket
    },
    timers,
    onState: (state, reason) => states.push([state, reason]),
    onStatus: (s) => statuses.push(s),
    onFrame: (f) => frames.push(f),
  })
  const last = () => sockets[sockets.length - 1]
  return { connection, sockets, timers, states, statuses, frames, last }
}

describe('viewerSocketUrl', () => {
  it('follows the page scheme and host', () => {
    expect(viewerSocketUrl({ protocol: 'https:', host: 'mac.tail.ts.net:8443' })).toBe('wss://mac.tail.ts.net:8443/ws/viewer')
    expect(viewerSocketUrl({ protocol: 'http:', host: 'localhost:5174' })).toBe('ws://localhost:5174/ws/viewer')
  })
})

describe('backoffMs', () => {
  it('doubles from 0.5 s up to 5 s', () => {
    expect([0, 1, 2, 3, 4, 5].map(backoffMs)).toEqual([500, 1000, 2000, 4000, 5000, 5000])
  })
})

describe('connectViewer', () => {
  it('connects, reports online, and passes on statuses and frames', () => {
    const t = setup()
    expect(t.last().binaryType).toBe('arraybuffer')
    t.last().open()
    t.last().sendStatus()
    t.last().receive(JSON.stringify({ type: 'pong' }))
    t.last().receive(new ArrayBuffer(3))
    t.last().receive(42)
    expect(t.states).toEqual([
      ['connecting', null],
      ['online', null],
    ])
    expect(t.statuses).toHaveLength(1)
    expect(t.frames).toHaveLength(1)
  })

  it('reports a dropped socket and retries with growing delays', () => {
    const t = setup()
    t.last().drop()
    expect(t.states.at(-1)).toEqual(['offline', 'connection lost'])
    t.timers.fire(500)
    t.last().drop()
    expect(t.timers.delays()).toContain(1000)
    expect(t.sockets).toHaveLength(2)
  })

  it('does not reset the backoff when a socket opens and is closed with 4429', () => {
    const t = setup()
    t.last().drop()
    t.timers.fire(500)
    t.last().open()
    t.last().drop(4429)
    expect(t.states.at(-1)).toEqual(['offline', 'too many screens are connected'])
    expect(t.timers.delays()).toContain(1000)
  })

  it('resets the backoff once the service sends something', () => {
    const t = setup()
    t.last().drop()
    t.timers.fire(500)
    t.last().receive(JSON.stringify(statusMessage()))
    t.last().drop()
    expect(t.timers.delays()).toContain(500)
  })

  it('keeps a single watchdog timer through a second of 30 Hz statuses and frames', () => {
    const t = setup()
    t.last().open()
    for (let i = 0; i < 30; i++) {
      t.last().sendStatus({ frame: { seq: i, fresh: true } })
      t.last().receive(new ArrayBuffer(8))
    }
    expect(t.statuses).toHaveLength(30)
    expect(t.frames).toHaveLength(30)
    expect(t.timers.delays()).toEqual([SILENCE_TIMEOUT_MS])
  })

  it('gives up on a socket that goes silent and reconnects', () => {
    const t = setup()
    const first = t.last()
    first.open()
    first.sendStatus()
    t.timers.fire(SILENCE_TIMEOUT_MS)
    expect(first.closed).toBe(true)
    expect(t.states.at(-1)).toEqual(['offline', 'no usable status from the service'])
    // A close event from the abandoned socket must not schedule a second retry.
    first.drop()
    expect(t.timers.delays().filter((ms) => ms === 500)).toHaveLength(1)
  })

  it('lets the watchdog fire when every status is unusable', () => {
    const t = setup()
    const first = t.last()
    first.open()
    t.timers.fire(SILENCE_TIMEOUT_MS) // the watchdog armed when it connected
    expect(first.closed).toBe(true)
    t.timers.fire(500)
    const second = t.last()
    second.open()
    second.receive(JSON.stringify({ type: 'status', phone_connected: 'yes' }))
    second.receive(new ArrayBuffer(2))
    // Neither reset the watchdog, so it is still the one armed on connect.
    expect(t.timers.delays()).toEqual([SILENCE_TIMEOUT_MS])
    t.timers.fire(SILENCE_TIMEOUT_MS)
    expect(second.closed).toBe(true)
  })

  it('stops for good when closed', () => {
    const t = setup()
    const socket = t.last()
    t.connection.close()
    expect(socket.closed).toBe(true)
    expect(t.timers.pending.size).toBe(0)
    socket.drop()
    expect(t.sockets).toHaveLength(1)
  })

  it('cancels a pending retry when closed', () => {
    const t = setup()
    t.last().drop()
    t.connection.close()
    expect(t.timers.pending.size).toBe(0)
  })
})
