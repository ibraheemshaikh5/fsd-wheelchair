import type { ButtonHTMLAttributes } from 'react'

// className is left out so a caller cannot replace the base classes by accident.
interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  /** primary: the main action. quiet: secondary. stop: halts the chair. */
  variant: 'primary' | 'quiet' | 'stop'
  /** xl: the full-width drive control. wide: fills its row in a dialog or card. */
  size?: 'md' | 'wide' | 'xl'
}

const SIZE_CLASS = { md: '', wide: 'btn-wide', xl: 'btn-xl' }

export function Button({ variant, size = 'md', type = 'button', ...rest }: Props) {
  const className = ['btn', `btn-${variant}`, SIZE_CLASS[size]].filter(Boolean).join(' ')
  return <button type={type} className={className} {...rest} />
}
