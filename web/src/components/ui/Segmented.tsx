// Segmented control with an animated selection pill.
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { cn } from '../../lib/format'

export interface SegmentOption<T extends string> {
  value: T
  label?: ReactNode
  icon?: ReactNode
  tip?: string
  kbd?: string
  disabled?: boolean
}

export interface SegmentedProps<T extends string> {
  value: T | null
  options: SegmentOption<T>[]
  onChange: (v: T) => void
  size?: 'xs' | 'sm' | 'md'
  className?: string
  fill?: boolean
  ariaLabel?: string
}

export function Segmented<T extends string>({ value, options, onChange, size = 'sm', className, fill, ariaLabel }: SegmentedProps<T>) {
  const wrap = useRef<HTMLDivElement>(null)
  const [pill, setPill] = useState<{ left: number; width: number } | null>(null)
  const index = options.findIndex((o) => o.value === value)

  useLayoutEffect(() => {
    const el = wrap.current
    if (!el) return
    const update = () => {
      const btn = el.querySelectorAll<HTMLButtonElement>('[data-seg]')[index]
      setPill(btn ? { left: btn.offsetLeft, width: btn.offsetWidth } : null)
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [index, options.length])

  return (
    <div
      ref={wrap}
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        'relative inline-flex items-center rounded-[7px] border border-line bg-surface-0 p-[2px]',
        fill && 'flex w-full',
        className,
      )}
    >
      {pill && (
        <div
          aria-hidden
          className="absolute bottom-[2px] top-[2px] rounded-[5px] bg-surface-4 shadow-[0_1px_0_rgb(255_255_255/0.07)_inset,0_1px_3px_rgb(0_0_0/0.4)] transition-[left,width] duration-200 ease-[var(--ease-out-expo)]"
          style={{ left: pill.left, width: pill.width }}
        />
      )}
      {options.map((o) => {
        const active = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            data-seg=""
            disabled={o.disabled}
            data-tip={o.tip}
            aria-label={o.label == null || typeof o.label !== 'string' ? o.tip : undefined}
            data-tip-kbd={o.kbd}
            onClick={() => onChange(o.value)}
            className={cn(
              'relative z-[1] inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-[5px] font-medium transition-colors duration-150 disabled:opacity-35',
              fill && 'flex-1',
              size === 'xs' && 'h-5 px-1.5 text-3xs [&_svg]:h-3 [&_svg]:w-3',
              size === 'sm' && 'h-6 px-2 text-2xs [&_svg]:h-3.5 [&_svg]:w-3.5',
              size === 'md' && 'h-7 px-2.5 text-xs [&_svg]:h-4 [&_svg]:w-4',
              active ? 'text-fg' : 'text-fg-3 hover:text-fg-2',
            )}
          >
            {o.icon}
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
