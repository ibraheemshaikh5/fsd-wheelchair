import type { ReactNode } from 'react'
import type { ManeuverKind } from '../types'

interface IconProps {
  size?: number
}

/** Line icon on a 24-unit grid, drawn in the current text colour. */
function LineIcon({ size = 22, weight = 2, children }: IconProps & { weight?: number; children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={weight}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

/** Solid icon on a 24-unit grid, filled with the current text colour. */
function SolidIcon({ size = 22, children }: IconProps & { children: ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="currentColor" aria-hidden="true">
      {children}
    </svg>
  )
}

export const SearchIcon = (props: IconProps) => (
  <LineIcon {...props}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m16 16 4.5 4.5" />
  </LineIcon>
)

export const MicIcon = (props: IconProps) => (
  <LineIcon {...props}>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
  </LineIcon>
)

export const CameraIcon = (props: IconProps) => (
  <LineIcon {...props}>
    <rect x="3" y="7" width="13" height="10" rx="2.5" />
    <path d="m16 11 5-3v8l-5-3" />
  </LineIcon>
)

export const SteeringIcon = (props: IconProps) => (
  <LineIcon {...props}>
    <circle cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="2" />
    <path d="M3.5 10.5c3-1.4 5.6-1.6 8.5-1.6s5.5.2 8.5 1.6M12 14v7" />
  </LineIcon>
)

export const CheckIcon = ({ size = 20 }: IconProps) => (
  <LineIcon size={size} weight={2.6}>
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  </LineIcon>
)

export const CloseIcon = ({ size = 20 }: IconProps) => (
  <LineIcon size={size} weight={2.2}>
    <path d="m6 6 12 12M18 6 6 18" />
  </LineIcon>
)

export const LocationIcon = (props: IconProps) => (
  <SolidIcon {...props}>
    <path d="M20.5 3.5 3.8 10.6c-.8.3-.7 1.5.1 1.7l6.3 1.5 1.5 6.3c.2.8 1.4.9 1.7.1L20.5 3.5Z" />
  </SolidIcon>
)

export const PinIcon = ({ size = 18 }: IconProps) => (
  <SolidIcon size={size}>
    <path d="M12 2a7 7 0 0 0-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 0 0-7-7Zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5Z" />
  </SolidIcon>
)

export function BatteryIcon({ pct }: { pct: number }) {
  const level = Math.max(0, Math.min(100, pct))
  return (
    <svg viewBox="0 0 34 16" width="34" height="16" aria-hidden="true">
      <rect x="1" y="1" width="28" height="14" rx="3.5" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.55" />
      <rect x="30.5" y="5" width="2.5" height="6" rx="1" fill="currentColor" opacity="0.55" />
      <rect x="3" y="3" width={(24 * level) / 100} height="10" rx="2" fill="currentColor" />
    </svg>
  )
}

// Turn arrows on a 32-unit grid.
const ARROWS: Record<Exclude<ManeuverKind, 'arrive'>, string> = {
  straight: 'M16 27V6M9 13l7-7 7 7',
  left: 'M22 27V15a4 4 0 0 0-4-4H7M13 5l-6 6 6 6',
  right: 'M10 27V15a4 4 0 0 1 4-4h11M19 5l6 6-6 6',
  'slight-left': 'M19 27v-9L9 8M9 16V8h8',
  'slight-right': 'M13 27v-9l10-10M15 8h8v8',
  'u-turn': 'M22 27V12a6 6 0 0 0-12 0v9M5 16l5 5 5-5',
}

export function ManeuverIcon({ kind, size = 44 }: { kind: ManeuverKind; size?: number }) {
  if (kind === 'arrive') return <PinIcon size={size} />
  return (
    <svg
      viewBox="0 0 32 32"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={ARROWS[kind]} />
    </svg>
  )
}
