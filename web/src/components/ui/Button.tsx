import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react'
import { LoaderCircle } from 'lucide-react'
import { cn } from '../../lib/format'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'subtle'
type Size = 'xs' | 'sm' | 'md' | 'lg'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  icon?: ReactNode
  iconRight?: ReactNode
  loading?: boolean
  tipLabel?: string
  tipKbd?: string
  ref?: Ref<HTMLButtonElement>
}

const VARIANTS: Record<Variant, string> = {
  primary:
    'text-white accent-gradient shadow-[0_1px_0_rgb(255_255_255/0.25)_inset,0_6px_18px_-6px_rgb(123_102_255/0.7)] hover:brightness-110 active:brightness-95',
  secondary: 'bg-surface-3 text-fg border border-line-2 hover:bg-surface-4 hover:border-line-3 active:bg-surface-3',
  subtle: 'bg-white/[0.04] text-fg-2 hover:bg-white/[0.08] hover:text-fg',
  ghost: 'text-fg-2 hover:bg-white/[0.06] hover:text-fg',
  danger: 'bg-bad/12 text-bad border border-bad/25 hover:bg-bad/20',
}

const SIZES: Record<Size, string> = {
  xs: 'h-6 px-2 text-2xs gap-1 rounded-[5px] [&_svg]:h-3 [&_svg]:w-3',
  sm: 'h-7 px-2.5 text-xs gap-1.5 rounded-md [&_svg]:h-3.5 [&_svg]:w-3.5',
  md: 'h-8 px-3 text-xs gap-2 rounded-md [&_svg]:h-4 [&_svg]:w-4',
  lg: 'h-10 px-4 text-[13px] gap-2 rounded-lg [&_svg]:h-4 [&_svg]:w-4',
}

export function Button({
  variant = 'secondary',
  size = 'sm',
  icon,
  iconRight,
  loading,
  className,
  children,
  disabled,
  tipLabel,
  tipKbd,
  ref,
  ...rest
}: ButtonProps) {
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled || loading}
      data-tip={tipLabel}
      data-tip-kbd={tipKbd}
      className={cn(
        'inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap font-medium transition-[background-color,border-color,color,filter,box-shadow,transform] duration-150 active:translate-y-[0.5px] disabled:pointer-events-none disabled:opacity-45',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading ? <LoaderCircle className="animate-spin" /> : icon}
      {children}
      {iconRight}
    </button>
  )
}

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string
  kbd?: string
  tipSide?: 'top' | 'bottom' | 'left' | 'right'
  active?: boolean
  size?: 'xs' | 'sm' | 'md'
  ref?: Ref<HTMLButtonElement>
}

export function IconButton({ label, kbd, tipSide, active, size = 'sm', className, children, ref, ...rest }: IconButtonProps) {
  return (
    <button
      ref={ref}
      type="button"
      aria-label={label}
      aria-pressed={active}
      data-tip={label}
      data-tip-kbd={kbd}
      data-tip-side={tipSide}
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-md transition-colors duration-150 disabled:pointer-events-none disabled:opacity-35',
        size === 'xs' && 'h-5 w-5 [&_svg]:h-3 [&_svg]:w-3',
        size === 'sm' && 'h-7 w-7 [&_svg]:h-[15px] [&_svg]:w-[15px]',
        size === 'md' && 'h-8 w-8 [&_svg]:h-4 [&_svg]:w-4',
        active ? 'bg-accent/18 text-fg ring-1 ring-inset ring-accent/40' : 'text-fg-3 hover:bg-white/[0.07] hover:text-fg',
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
}
