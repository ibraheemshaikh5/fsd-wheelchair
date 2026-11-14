// Shared fixtures for the live service tests.

import type { Place, Route } from '../../types'
import { offset } from '../../lib/geo'
import { polylineLength, stepsFromGeometry } from '../../lib/route'
import type { ServiceStatus } from './status'
import type { Timers, ViewerSocket } from './viewerSocket'

export const ORIGIN = { lat: 38.987, lng: -76.9428 }

/** 100 m north, then 50 m east: one right turn. */
export function lRoute(): { route: Route; place: Place } {
  const corner = offset(ORIGIN, 0, 100)
  const end = offset(corner, 90, 50)
  const points = [ORIGIN, corner, end]
  const place: Place = { id: 'cafe', name: 'Cafe', location: end, distanceM: 120 }
  return { place, route: { points, steps: stepsFromGeometry(points, place.name), lengthM: polylineLength(points) } }
}

export function statusMessage(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'status',
    phone_connected: true,
    clock_synced: true,
    sensors_ok: true,
    frame: { seq: 7, w: 640, h: 480, age_ms: 80, fresh: true },
    gps: {
      lat: ORIGIN.lat,
      lon: ORIGIN.lng,
      accuracy_m: 4,
      altitude_m: 40,
      speed_mps: 0,
      course_deg: null,
      age_ms: 300,
      fresh: true,
    },
    heading: { deg: 90, source: 'compass', reference: 'true', declination_deg: -11, compass_deg: 101 },
    stats: { fps: 9.5, frames: 100, missed: 0, rejected: 0, rate_limited: 0 },
    recording: { active: false },
    destination: null,
    ...overrides,
  }
}

export function status(overrides: Record<string, unknown> = {}): ServiceStatus {
  return statusMessage(overrides) as unknown as ServiceStatus
}

export class FakeSocket implements ViewerSocket {
  binaryType: BinaryType = 'blob'
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  closed = false
  constructor(readonly url: string) {}
  open() {
    this.onopen?.({} as Event)
  }
  receive(data: unknown) {
    this.onmessage?.({ data } as MessageEvent)
  }
  sendStatus(overrides: Record<string, unknown> = {}) {
    this.receive(JSON.stringify(statusMessage(overrides)))
  }
  drop(code = 1006) {
    this.onclose?.({ code } as CloseEvent)
  }
  close() {
    this.closed = true
  }
}

/** Timers that only run when the test says so. */
export class ManualTimers implements Timers {
  private next = 1
  readonly pending = new Map<number, { fn: () => void; ms: number }>()
  set = (fn: () => void, ms: number) => {
    const id = this.next++
    this.pending.set(id, { fn, ms })
    return id
  }
  clear = (id: number) => {
    this.pending.delete(id)
  }
  /** Runs the pending timer with the given delay (the newest one if several). */
  fire(ms: number): void {
    const entry = [...this.pending.entries()].reverse().find(([, t]) => t.ms === ms)
    if (!entry) throw new Error(`no ${ms} ms timer pending (have ${[...this.pending.values()].map((t) => t.ms)})`)
    this.pending.delete(entry[0])
    entry[1].fn()
  }
  delays(): number[] {
    return [...this.pending.values()].map((t) => t.ms)
  }
}
