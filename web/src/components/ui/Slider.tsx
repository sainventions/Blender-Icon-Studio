// Custom slider: gradient fill, default marker, pointer-capture dragging, keyboard, double-click reset.
import { useRef, useState } from 'react'
import { clamp, cn } from '../../lib/format'
import type { ChangePhase } from './NumberField'

export interface SliderProps {
  value: number
  min: number
  max: number
  step?: number
  onChange: (v: number, phase: ChangePhase) => void
  defaultValue?: number
  disabled?: boolean
  className?: string
  ariaLabel?: string
  /** Optional CSS background for the track (e.g. a hue or gradient preview). */
  trackBackground?: string
  /** Fill from this value instead of min (bipolar sliders). */
  origin?: number
}

export function Slider({ value, min, max, step = 0.01, onChange, defaultValue, disabled, className, ariaLabel, trackBackground, origin }: SliderProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [dragging, setDragging] = useState(false)
  const lastRef = useRef(value)
  const span = max - min || 1
  const t = clamp((value - min) / span, 0, 1)
  const o = clamp(((origin ?? min) - min) / span, 0, 1)
  const fillL = Math.min(t, o)
  const fillW = Math.abs(t - o)

  const quantize = (v: number) => {
    const q = Math.round((v - min) / step) * step + min
    return Number(clamp(q, min, max).toFixed(6))
  }
  const fromPointer = (clientX: number) => {
    const r = trackRef.current!.getBoundingClientRect()
    return quantize(min + clamp((clientX - r.left) / r.width, 0, 1) * span)
  }

  return (
    <div
      className={cn('group relative flex h-6 min-w-0 flex-1 touch-none items-center', disabled && 'pointer-events-none opacity-40', className)}
      onPointerDown={(e) => {
        if (e.button !== 0) return
        e.preventDefault()
        ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
        ;(e.currentTarget as HTMLElement).focus()
        setDragging(true)
        const v = fromPointer(e.clientX)
        lastRef.current = v
        if (v !== value) onChange(v, 'change')
      }}
      onPointerMove={(e) => {
        if (!dragging) return
        const v = fromPointer(e.clientX)
        if (v !== lastRef.current) {
          lastRef.current = v
          onChange(v, 'change')
        }
      }}
      onPointerUp={() => {
        if (!dragging) return
        setDragging(false)
        onChange(lastRef.current, 'commit')
      }}
      onPointerCancel={() => setDragging(false)}
      onDoubleClick={() => defaultValue !== undefined && onChange(defaultValue, 'commit')}
      role="slider"
      tabIndex={disabled ? -1 : 0}
      aria-label={ariaLabel}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      onKeyDown={(e) => {
        const mult = e.shiftKey ? 10 : 1
        if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
          e.preventDefault()
          e.stopPropagation()
          onChange(quantize(value + step * mult), 'commit')
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
          e.preventDefault()
          e.stopPropagation()
          onChange(quantize(value - step * mult), 'commit')
        } else if (e.key === 'Home') {
          onChange(min, 'commit')
        } else if (e.key === 'End') {
          onChange(max, 'commit')
        }
      }}
    >
      <div
        ref={trackRef}
        className="relative h-[4px] w-full overflow-visible rounded-full bg-white/[0.08]"
        style={trackBackground ? { background: trackBackground } : undefined}
      >
        {!trackBackground && (
          <div
            className="absolute inset-y-0 rounded-full accent-gradient opacity-90 transition-opacity group-hover:opacity-100"
            style={{ left: `${fillL * 100}%`, width: `${fillW * 100}%` }}
          />
        )}
        {defaultValue !== undefined && defaultValue > min && defaultValue < max && (
          <div
            className="absolute top-1/2 h-[8px] w-px -translate-y-1/2 bg-white/25"
            style={{ left: `${((defaultValue - min) / span) * 100}%` }}
          />
        )}
      </div>
      <div
        className={cn(
          'pointer-events-none absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border border-black/30 bg-white shadow-[0_1px_4px_rgb(0_0_0/0.5)] transition-[transform,box-shadow] duration-150 group-focus-visible:shadow-[0_0_0_3px_rgb(143_125_255/0.45)]',
          dragging ? 'scale-110 shadow-[0_0_0_4px_rgb(143_125_255/0.3)]' : 'group-hover:scale-110',
        )}
        style={{ left: `${t * 100}%` }}
      />
    </div>
  )
}
