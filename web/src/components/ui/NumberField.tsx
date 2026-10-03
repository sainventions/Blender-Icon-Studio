// Numeric input with expression support ("35*3", "*2", "+0.1"), arrow-key stepping and scrubbable labels.
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { clamp, cn, decimalsForStep, formatNumber } from '../../lib/format'
import { evaluateExpression } from '../../lib/expr'

export type ChangePhase = 'change' | 'commit'

export interface NumberFieldProps {
  value: number
  onChange: (v: number, phase: ChangePhase) => void
  min?: number
  max?: number
  step?: number
  /** Display multiplier (100 → show 0.5 as 50). */
  scale?: number
  decimals?: number
  unit?: string
  prefix?: ReactNode
  disabled?: boolean
  className?: string
  inputClassName?: string
  /** Allow values outside min/max when typed (soft limits). */
  soft?: boolean
  ariaLabel?: string
  mixed?: boolean
}

export function NumberField({
  value,
  onChange,
  min = -Infinity,
  max = Infinity,
  step = 0.01,
  scale = 1,
  decimals,
  unit,
  prefix,
  disabled,
  className,
  inputClassName,
  soft,
  ariaLabel,
  mixed,
}: NumberFieldProps) {
  const dec = decimals ?? Math.max(0, decimalsForStep(step * scale))
  const shown = value * scale
  const [text, setText] = useState(() => formatNumber(shown, dec))
  const [focused, setFocused] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!focused) setText(mixed ? '' : formatNumber(shown, dec))
  }, [shown, dec, focused, mixed])

  const lim = (v: number) => (soft ? v : clamp(v, min, max))

  const commitText = () => {
    const v = evaluateExpression(text, shown)
    if (v == null) {
      setText(formatNumber(shown, dec))
      return
    }
    const next = lim(v / scale)
    if (next !== value) onChange(next, 'commit')
    setText(formatNumber(next * scale, dec))
  }

  const stepBy = (dir: number, e: React.KeyboardEvent) => {
    const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1
    const next = lim(Math.round((value + dir * step * mult) / (step * (e.altKey ? 0.1 : 1))) * step * (e.altKey ? 0.1 : 1))
    onChange(next, 'commit')
    setText(formatNumber(next * scale, dec))
  }

  return (
    <label
      className={cn(
        'group relative flex h-6 min-w-0 items-center rounded-md border border-line bg-surface-0 transition-[border-color,box-shadow,background-color] duration-150 focus-within:border-accent/70 focus-within:bg-surface-1 focus-within:shadow-[0_0_0_3px_rgb(143_125_255/0.15)] hover:border-line-2',
        disabled && 'pointer-events-none opacity-45',
        className,
      )}
    >
      {prefix && <span className="flex shrink-0 items-center pl-1.5 text-3xs font-semibold text-fg-4">{prefix}</span>}
      <input
        ref={inputRef}
        type="text"
        inputMode="decimal"
        aria-label={ariaLabel}
        disabled={disabled}
        value={text}
        placeholder={mixed ? 'Mixed' : undefined}
        spellCheck={false}
        onFocus={(e) => {
          setFocused(true)
          requestAnimationFrame(() => e.target.select())
        }}
        onBlur={() => {
          setFocused(false)
          commitText()
        }}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            commitText()
            inputRef.current?.blur()
          } else if (e.key === 'Escape') {
            setText(formatNumber(shown, dec))
            requestAnimationFrame(() => inputRef.current?.blur())
          } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            stepBy(1, e)
          } else if (e.key === 'ArrowDown') {
            e.preventDefault()
            stepBy(-1, e)
          }
          e.stopPropagation()
        }}
        className={cn(
          'h-full w-full min-w-0 bg-transparent px-1.5 text-right text-2xs tabular text-fg outline-none placeholder:text-fg-4',
          inputClassName,
        )}
      />
      {unit && <span className="pointer-events-none shrink-0 pr-1.5 text-3xs text-fg-4">{unit}</span>}
    </label>
  )
}

/** A label you can drag horizontally to scrub a value (Shift ×10, Alt ×0.1). Double-click resets. */
export function ScrubLabel({
  children,
  value,
  onChange,
  step = 0.01,
  min = -Infinity,
  max = Infinity,
  pixelsPerStep = 2,
  disabled,
  onReset,
  className,
  title,
}: {
  children: ReactNode
  value: number
  onChange: (v: number, phase: ChangePhase) => void
  step?: number
  min?: number
  max?: number
  pixelsPerStep?: number
  disabled?: boolean
  onReset?: () => void
  className?: string
  title?: string
}) {
  const drag = useRef<{ x: number; v: number; moved: boolean; last: number } | null>(null)
  const [active, setActive] = useState(false)

  return (
    <span
      title={title}
      onPointerDown={(e) => {
        if (disabled || e.button !== 0) return
        e.preventDefault()
        ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
        drag.current = { x: e.clientX, v: value, moved: false, last: value }
      }}
      onPointerMove={(e) => {
        const d = drag.current
        if (!d) return
        const dx = e.clientX - d.x
        if (!d.moved && Math.abs(dx) < 3) return
        if (!d.moved) setActive(true)
        d.moved = true
        const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1
        const raw = d.v + (dx / pixelsPerStep) * step * mult
        const q = step * (e.altKey ? 0.1 : 1)
        const next = clamp(Math.round(raw / q) * q, min, max)
        if (next !== d.last) {
          d.last = next
          onChange(Number(next.toFixed(6)), 'change')
        }
      }}
      onPointerUp={(e) => {
        const d = drag.current
        drag.current = null
        setActive(false)
        ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
        if (d?.moved) onChange(d.last, 'commit')
      }}
      onDoubleClick={() => !disabled && onReset?.()}
      className={cn(
        'select-none truncate',
        !disabled && 'cursor-ew-resize',
        active ? 'text-fg' : 'text-fg-3 hover:text-fg-2',
        className,
      )}
    >
      {children}
    </span>
  )
}
