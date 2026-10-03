// Multi-stop gradient editor: draggable stops, click-to-add, drag-off/Delete to remove, per-stop colour,
// opacity and position, reverse, plus an angle dial for linear gradients.
import { useRef, useState } from 'react'
import { ArrowLeftRight, Trash2 } from 'lucide-react'
import type { GradientStop } from '../../types'
import { clamp, cn } from '../../lib/format'
import { rgba, sampleStops, stopsToCss } from '../../lib/color'
import { ColorField } from './ColorField'
import { NumberField, type ChangePhase } from './NumberField'
import { IconButton } from './Button'

export function GradientEditor({
  stops,
  onChange,
  angle,
  onAngleChange,
  disabled,
}: {
  stops: GradientStop[]
  onChange: (stops: GradientStop[], phase: ChangePhase) => void
  angle?: number
  onAngleChange?: (deg: number, phase: ChangePhase) => void
  disabled?: boolean
}) {
  const barRef = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState(0)
  const drag = useRef<{ index: number; startY: number; removed: boolean; moved: boolean } | null>(null)
  const sel = Math.min(selected, stops.length - 1)
  const stop = stops[sel]

  const setStop = (i: number, patch: Partial<GradientStop>, phase: ChangePhase) =>
    onChange(stops.map((s, j) => (j === i ? { ...s, ...patch } : s)), phase)

  const offsetAt = (clientX: number) => {
    const r = barRef.current!.getBoundingClientRect()
    return clamp((clientX - r.left) / r.width, 0, 1)
  }

  const remove = (i: number) => {
    if (stops.length <= 2) return
    onChange(stops.filter((_, j) => j !== i), 'commit')
    setSelected(Math.max(0, i - 1))
  }

  return (
    <div className={cn('space-y-2', disabled && 'pointer-events-none opacity-45')}>
      <div className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1 pb-3">
          <div
            ref={barRef}
            className="relative h-5 cursor-copy overflow-hidden rounded-md border border-line-2 checkerboard-sm"
            onPointerDown={(e) => {
              if (e.target !== e.currentTarget && !(e.target as HTMLElement).dataset.bar) return
              const t = Number(offsetAt(e.clientX).toFixed(3))
              const s = sampleStops(stops, t)
              const next = [...stops, { offset: t, color: s.color, opacity: s.opacity }]
              onChange(next, 'commit')
              setSelected(next.length - 1)
            }}
            data-tip="Click to add a stop"
          >
            <div data-bar="1" className="absolute inset-0" style={{ background: `linear-gradient(90deg, ${stopsToCss(stops)})` }} />
          </div>
          {stops.map((s, i) => (
            <button
              key={i}
              type="button"
              aria-label={`Stop ${i + 1}`}
              onPointerDown={(e) => {
                e.stopPropagation()
                e.preventDefault()
                ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
                setSelected(i)
                drag.current = { index: i, startY: e.clientY, removed: false, moved: false }
              }}
              onPointerMove={(e) => {
                const d = drag.current
                if (!d || d.index !== i) return
                const off = Number(offsetAt(e.clientX).toFixed(3))
                const away = Math.abs(e.clientY - d.startY) > 34 && stops.length > 2
                d.removed = away
                if (Math.abs(off - stops[i].offset) > 0.001) {
                  d.moved = true
                  setStop(i, { offset: off }, 'change')
                }
              }}
              onPointerUp={() => {
                const d = drag.current
                drag.current = null
                if (!d) return
                if (d.removed) remove(i)
                else if (d.moved) onChange(stops, 'commit')
              }}
              onKeyDown={(e) => {
                if (e.key === 'Delete' || e.key === 'Backspace') {
                  e.preventDefault()
                  e.stopPropagation()
                  remove(i)
                }
              }}
              className={cn(
                'absolute top-[14px] h-[13px] w-[13px] -translate-x-1/2 rounded-full border-2 shadow-[0_1px_4px_rgb(0_0_0/0.6)] transition-transform',
                i === sel ? 'z-[2] scale-110 border-white' : 'z-[1] border-white/60 hover:scale-110',
              )}
              style={{ left: `${s.offset * 100}%`, background: rgba(s.color, 1) }}
            />
          ))}
        </div>
        <IconButton
          label="Reverse gradient"
          size="sm"
          onClick={() => onChange(stops.map((s) => ({ ...s, offset: Number((1 - s.offset).toFixed(3)) })).reverse(), 'commit')}
        >
          <ArrowLeftRight />
        </IconButton>
      </div>

      {stop && (
        <div className="flex items-center gap-1.5">
          <ColorField
            value={stop.color}
            alpha={stop.opacity}
            onChange={(c, p) => setStop(sel, { color: c }, p)}
            onAlphaChange={(a, p) => setStop(sel, { opacity: a }, p)}
            className="min-w-0 flex-1"
          />
          <NumberField
            value={stop.offset}
            onChange={(v, p) => setStop(sel, { offset: v }, p)}
            min={0}
            max={1}
            step={0.01}
            scale={100}
            decimals={0}
            unit="%"
            className="w-[52px] shrink-0"
            ariaLabel="Stop position"
          />
          <IconButton label="Remove stop" size="sm" disabled={stops.length <= 2} onClick={() => remove(sel)}>
            <Trash2 />
          </IconButton>
        </div>
      )}

      {angle !== undefined && onAngleChange && (
        <div className="flex items-center gap-2">
          <span className="w-[84px] shrink-0 text-2xs text-fg-3">Angle</span>
          <AngleDial value={angle} onChange={onAngleChange} size={26} />
          <NumberField value={angle} onChange={onAngleChange} min={0} max={360} step={1} decimals={0} unit="°" className="w-[58px]" />
          <div className="flex gap-0.5">
            {[0, 90, 135, 180].map((a) => (
              <button
                key={a}
                type="button"
                onClick={() => onAngleChange(a, 'commit')}
                className={cn(
                  'h-5 rounded px-1 text-3xs tabular transition-colors',
                  Math.round(angle) === a ? 'bg-white/10 text-fg' : 'text-fg-4 hover:bg-white/[0.06] hover:text-fg-2',
                )}
              >
                {a}°
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** Compact circular angle control (0° = up, clockwise). Shift snaps to 15°. */
export function AngleDial({
  value,
  onChange,
  size = 28,
  disabled,
}: {
  value: number
  onChange: (deg: number, phase: ChangePhase) => void
  size?: number
  disabled?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)
  const last = useRef(value)
  const angleFrom = (e: React.PointerEvent, snap: boolean) => {
    const r = ref.current!.getBoundingClientRect()
    const dx = e.clientX - (r.left + r.width / 2)
    const dy = e.clientY - (r.top + r.height / 2)
    let deg = (Math.atan2(dx, -dy) * 180) / Math.PI
    deg = (deg + 360) % 360
    if (snap) deg = Math.round(deg / 15) * 15
    return Math.round(deg) % 360
  }
  const rad = (value * Math.PI) / 180
  const r = size / 2 - 3
  return (
    <div
      ref={ref}
      role="slider"
      aria-valuenow={value}
      tabIndex={0}
      className={cn('relative shrink-0 cursor-grab rounded-full border border-line-2 bg-surface-0 active:cursor-grabbing', disabled && 'pointer-events-none opacity-40')}
      style={{ width: size, height: size }}
      onPointerDown={(e) => {
        ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
        dragging.current = true
        last.current = angleFrom(e, e.shiftKey)
        onChange(last.current, 'change')
      }}
      onPointerMove={(e) => {
        if (!dragging.current) return
        const a = angleFrom(e, e.shiftKey)
        if (a !== last.current) {
          last.current = a
          onChange(a, 'change')
        }
      }}
      onPointerUp={() => {
        if (!dragging.current) return
        dragging.current = false
        onChange(last.current, 'commit')
      }}
    >
      <svg width={size} height={size} className="absolute inset-0">
        <line
          x1={size / 2}
          y1={size / 2}
          x2={size / 2 + Math.sin(rad) * r}
          y2={size / 2 - Math.cos(rad) * r}
          stroke="var(--color-accent)"
          strokeWidth={2}
          strokeLinecap="round"
        />
        <circle cx={size / 2 + Math.sin(rad) * r} cy={size / 2 - Math.cos(rad) * r} r={2.5} fill="white" />
        <circle cx={size / 2} cy={size / 2} r={1.5} fill="rgb(255 255 255 / 0.4)" />
      </svg>
    </div>
  )
}
