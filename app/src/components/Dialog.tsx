import { useEffect, type ReactNode } from 'react'

interface Props {
  /** Accessible name, or the id of the element that titles the dialog. */
  label?: string
  labelledBy?: string
  /** Called on Escape or a tap outside the sheet. Omit for a dialog that must be answered. */
  onDismiss?: () => void
  /** Frosted backdrop that hides the app, rather than a dim one. */
  opaque?: boolean
  className?: string
  children: ReactNode
}

/** Modal sheet centred over the app. */
export function Dialog({ label, labelledBy, onDismiss, opaque = false, className = '', children }: Props) {
  useEffect(() => {
    if (!onDismiss) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onDismiss()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onDismiss])

  return (
    <div
      className={`dialog-scrim ${opaque ? 'opaque' : ''}`}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      aria-labelledby={labelledBy}
      onClick={onDismiss}
    >
      <div className={`dialog-sheet ${className}`} onClick={(e) => e.stopPropagation()}>
        {children}
      </div>
    </div>
  )
}
