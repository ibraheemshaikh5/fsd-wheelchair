import type { ButtonHTMLAttributes } from 'react'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** primary: the main action. quiet: secondary. stop: halts the chair. */
  variant: 'primary' | 'quiet' | 'stop'
  /** xl: the full-width drive control. wide: fills its row in a dialog. */
  size?: 'md' | 'wide' | 'xl'
}

const SIZE_CLASS = { md: '', wide: 'btn-wide', xl: 'btn-xl' }

export function Button({ variant, size = 'md', className = '', type = 'button', ...rest }: Props) {
  return <button type={type} className={`btn btn-${variant} ${SIZE_CLASS[size]} ${className}`.trim()} {...rest} />
}
