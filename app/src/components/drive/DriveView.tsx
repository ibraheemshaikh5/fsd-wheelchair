import type { DriveState, LocalPoint, Obstacle, PathStatus } from '../../types'
import { ChairModel } from './ChairModel'

// A pinhole camera well behind and high above the chair, tilted down far enough
// that the horizon sits above the top of the view. That keeps the ground just
// ahead of the chair visible instead of hidden behind its backrest, and leaves
// things beside and just behind the chair in view.
const VIEW_W = 800
const VIEW_H = 1000
const HORIZON_Y = -130
const FOCAL = 1125
const CAM_HEIGHT_M = 6
const CAM_BACK_M = 9
const FOG_HEIGHT = 340

// The space the chair sweeps: its half-width plus a small margin.
const FOOTPRINT_HALF_WIDTH_M = 0.36
const FOOTPRINT_START_M = 0.45 // just ahead of the footplates
const RINGS = [
  { r: 0.9144, label: '3 ft' },
  { r: 1.8288, label: '6 ft' },
  { r: 4.572, label: '15 ft' },
]

const STATUS_COLOR: Record<PathStatus, string> = {
  clear: 'var(--blue)',
  slowing: 'var(--amber)',
  blocked: 'var(--red)',
}

/** Ground point (chair frame, metres) to screen. `s` is pixels per metre there. */
function project(x: number, z: number) {
  const s = FOCAL / Math.max(z + CAM_BACK_M, 1.5)
  return { X: VIEW_W / 2 + x * s, Y: HORIZON_Y + CAM_HEIGHT_M * s, s }
}

const pt = (x: number, z: number) => {
  const { X, Y } = project(x, z)
  return `${X.toFixed(1)},${Y.toFixed(1)}`
}

function ring(r: number): string {
  const pts: string[] = []
  for (let a = 0; a < 360; a += 5) {
    const rad = (a * Math.PI) / 180
    pts.push(pt(Math.sin(rad) * r, Math.cos(rad) * r))
  }
  return pts.join(' ')
}

/** Points every `step` metres along `path`, between `from` and `to` metres from its start. */
function resample(path: LocalPoint[], from: number, to: number, step: number): LocalPoint[] {
  const out: LocalPoint[] = []
  let walked = 0
  let want = from
  for (let i = 1; i < path.length && want <= to; i++) {
    const a = path[i - 1]
    const b = path[i]
    const len = Math.hypot(b.x - a.x, b.z - a.z)
    while (len > 0 && want <= walked + len && want <= to) {
      const t = (want - walked) / len
      out.push({ x: a.x + (b.x - a.x) * t, z: a.z + (b.z - a.z) * t })
      want += step
    }
    walked += len
  }
  return out
}

/**
 * The strip of ground the chair is about to cover: its own width swept along the
 * next few metres of the planned trajectory, fading with distance, ending in an arrow.
 */
function Footprint({ path, length, color }: { path: LocalPoint[]; length: number; color: string }) {
  const centre = resample(path, FOOTPRINT_START_M, length, 0.25)
  if (centre.length < 3) return null
  const edges = centre.map((p, i) => {
    const a = centre[Math.max(0, i - 1)]
    const b = centre[Math.min(centre.length - 1, i + 1)]
    const len = Math.hypot(b.x - a.x, b.z - a.z) || 1
    const tx = (b.x - a.x) / len
    const tz = (b.z - a.z) / len
    // (tz, -tx) points to the chair's right
    return {
      left: pt(p.x - tz * FOOTPRINT_HALF_WIDTH_M, p.z + tx * FOOTPRINT_HALF_WIDTH_M),
      right: pt(p.x + tz * FOOTPRINT_HALF_WIDTH_M, p.z - tx * FOOTPRINT_HALF_WIDTH_M),
      tip: pt(p.x + tx * 0.55, p.z + tz * 0.55),
    }
  })
  const last = edges.length - 1
  return (
    <g fill={color} stroke={color}>
      {edges.slice(0, last).map((e, i) => {
        const fade = 1 - i / last
        const next = edges[i + 1]
        return (
          <g key={i}>
            <polygon points={`${e.left} ${next.left} ${next.right} ${e.right}`} stroke="none" opacity={0.1 + 0.3 * fade} />
            <polyline points={`${e.left} ${next.left}`} fill="none" strokeWidth="3" opacity={0.35 + 0.6 * fade} />
            <polyline points={`${e.right} ${next.right}`} fill="none" strokeWidth="3" opacity={0.35 + 0.6 * fade} />
          </g>
        )
      })}
      <polygon points={`${edges[last].left} ${edges[last].tip} ${edges[last].right}`} stroke="none" opacity="0.55" />
    </g>
  )
}

function Person({ x, z, ringColor }: { x: number; z: number; ringColor: string | null }) {
  const { X, Y, s } = project(x, z)
  const fade = Math.max(0, Math.min(1, (22 - z) / 8))
  return (
    <g opacity={fade}>
      {ringColor && (
        <ellipse cx={X} cy={Y} rx={0.55 * s} ry={0.55 * s * 0.42} fill={ringColor} fillOpacity="0.16" stroke={ringColor} strokeWidth="3" />
      )}
      <ellipse cx={X} cy={Y} rx={0.3 * s} ry={0.1 * s} fill="#000" opacity="0.14" />
      <rect x={X - 0.2 * s} y={Y - 1.45 * s} width={0.4 * s} height={1.45 * s} rx={0.18 * s} fill="var(--viz-obstacle)" />
      <circle cx={X} cy={Y - 1.62 * s} r={0.14 * s} fill="var(--viz-obstacle)" />
    </g>
  )
}

interface Props {
  state: DriveState
  /** The pane is much wider than it is tall: fit the height and let the ground run off the sides. */
  wide?: boolean
}

export function DriveView({ state, wide = false }: Props) {
  const driving = state.mode === 'driving'
  const color = driving ? STATUS_COLOR[state.pathStatus] : 'var(--viz-idle-line)'
  // Reach further ahead the faster the chair is going.
  const reach = driving ? Math.min(6.5, Math.max(3.2, 2.6 + state.speedMps * 2.6)) : 3.2
  const obstacles = [...state.obstacles].sort((a, b) => b.z - a.z)
  const chair = project(0, 0)
  const person = (o: Obstacle) => (
    <Person key={o.id} x={o.x} z={o.z} ringColor={o.id === state.yieldingToId ? STATUS_COLOR[state.pathStatus] : null} />
  )

  return (
    <svg
      className="drive-view"
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      preserveAspectRatio={wide ? 'xMidYMid meet' : 'xMidYMid slice'}
      role="img"
      aria-label="View of the chair and what is around it"
    >
      <defs>
        <linearGradient id="fog" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--viz-bg)" stopOpacity="1" />
          <stop offset="0.45" stopColor="var(--viz-bg)" stopOpacity="0.95" />
          <stop offset="1" stopColor="var(--viz-bg)" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* Oversized, so the ground carries on when the pane is wider than the view. */}
      <rect x={-2400} width={VIEW_W + 4800} height={VIEW_H} fill="var(--viz-ground)" />

      <g fill="none" stroke="var(--viz-ring)" strokeWidth="2">
        {RINGS.map(({ r }) => (
          <polygon key={r} points={ring(r)} />
        ))}
      </g>
      <g className="ring-labels">
        {RINGS.map(({ r, label }) => {
          const { X, Y } = project(r, 0)
          return (
            <text key={r} x={X + 8} y={Y + 5}>
              {label}
            </text>
          )
        })}
      </g>

      <Footprint path={state.path} length={reach} color={color} />

      {obstacles.filter((o) => o.z >= 0).map(person)}
      <rect x={-2400} width={VIEW_W + 4800} height={FOG_HEIGHT} fill="url(#fog)" />
      <ChairModel x={chair.X} y={chair.Y} pxPerMetre={chair.s} />
      {obstacles.filter((o) => o.z < 0).map(person)}
    </svg>
  )
}
