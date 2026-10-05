// Light-angle dial: 0° = light from the top, positive = clockwise (PLAN §2). Drag the sun (Shift snaps to 15°),
// click anywhere on the dial, use the arrow keys (Shift ×15, PageUp/PageDown ±45, Home = top), double-click to
// reset to the Icon Composer default (−45°). The mini plate in the centre is lit from the dialled direction.
import { useCallback, useId, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { plateOutlinePath } from '../lib/shapes'

export interface LightDialProps {
  angle: number
  onChange: (deg: number) => void
  size?: number
}

export const DEFAULT_LIGHT_ANGLE = -45

/** Normalise to (−180, 180]. */
export function normalizeAngle(deg: number): number {
  let a = ((((deg + 180) % 360) + 360) % 360) - 180
  if (a === -180) a = 180
  return Math.round(a * 10) / 10
}

function compass(a: number): string {
  const names = ['top', 'top-right', 'right', 'bottom-right', 'bottom', 'bottom-left', 'left', 'top-left']
  const i = Math.round((((a % 360) + 360) % 360) / 45) % 8
  return names[i]
}

const C = 50
const R_TRACK = 35
const SQUIRCLE = plateOutlinePath('squircle', 0, 30)

export function LightDial({ angle, onChange, size = 132 }: LightDialProps) {
  // React's ids contain characters (':', '«»') that break url(#…) references in some engines, so keep [A-Za-z0-9_-].
  const uid = 'ld' + useId().replace(/[^A-Za-z0-9_-]/g, '')
  const svgRef = useRef<SVGSVGElement>(null)
  const [dragging, setDragging] = useState(false)
  const [focused, setFocused] = useState(false)
  const a = normalizeAngle(Number.isFinite(angle) ? angle : DEFAULT_LIGHT_ANGLE)
  const rad = (a * Math.PI) / 180
  const sx = Math.sin(rad)
  const sy = -Math.cos(rad) // SVG y down
  const sun = { x: C + R_TRACK * sx, y: C + R_TRACK * sy }

  const emit = useCallback(
    (deg: number) => {
      const n = normalizeAngle(deg)
      if (n !== a) onChange(n)
    },
    [a, onChange],
  )

  const angleFromEvent = (e: ReactPointerEvent<SVGSVGElement>): number | null => {
    const el = svgRef.current
    if (!el) return null
    const r = el.getBoundingClientRect()
    const x = ((e.clientX - r.left) / r.width) * 100 - C
    const y = ((e.clientY - r.top) / r.height) * 100 - C
    if (Math.hypot(x, y) < 2) return null
    let deg = (Math.atan2(x, -y) * 180) / Math.PI
    deg = e.shiftKey ? Math.round(deg / 15) * 15 : Math.round(deg)
    return deg
  }

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    svgRef.current?.focus()
    e.currentTarget.setPointerCapture(e.pointerId)
    setDragging(true)
    const deg = angleFromEvent(e)
    if (deg !== null) emit(deg)
  }
  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragging) return
    const deg = angleFromEvent(e)
    if (deg !== null) emit(deg)
  }
  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragging) return
    e.currentTarget.releasePointerCapture(e.pointerId)
    setDragging(false)
  }

  const onKeyDown = (e: KeyboardEvent<SVGSVGElement>) => {
    const step = e.shiftKey ? 15 : 1
    let next: number | null = null
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowUp':
        next = a + step
        break
      case 'ArrowLeft':
      case 'ArrowDown':
        next = a - step
        break
      case 'PageUp':
        next = a + 45
        break
      case 'PageDown':
        next = a - 45
        break
      case 'Home':
        next = 0
        break
      case 'End':
        next = 180
        break
      default:
        return
    }
    e.preventDefault()
    emit(next)
  }

  // Signed arc from the top to the light angle.
  const arcEnd = sun
  const sweep = a >= 0 ? 1 : 0
  const arc =
    Math.abs(a) < 0.5
      ? ''
      : `M ${C} ${C - R_TRACK} A ${R_TRACK} ${R_TRACK} 0 0 ${sweep} ${arcEnd.x.toFixed(3)} ${arcEnd.y.toFixed(3)}`

  const ticks = []
  for (let d = 0; d < 360; d += 5) {
    const major = d % 45 === 0
    const mid = d % 15 === 0
    const len = major ? 4.2 : mid ? 2.8 : 1.6
    const r0 = 45.5
    const t = (d * Math.PI) / 180
    ticks.push(
      <line
        key={d}
        x1={C + r0 * Math.sin(t)}
        y1={C - r0 * Math.cos(t)}
        x2={C + (r0 - len) * Math.sin(t)}
        y2={C - (r0 - len) * Math.cos(t)}
        stroke="#ffffff"
        strokeOpacity={major ? 0.42 : mid ? 0.22 : 0.1}
        strokeWidth={major ? 0.9 : 0.6}
        strokeLinecap="round"
      />,
    )
  }

  // Mini plate lit from the dial direction: gradient along the light vector + rim highlight + offset shadow.
  const gx1 = 50 + 50 * sx
  const gy1 = 50 + 50 * sy
  const gx2 = 50 - 50 * sx
  const gy2 = 50 - 50 * sy
  const shadowDx = -sx * 2.6
  const shadowDy = -sy * 2.6
  const label = `${a > 0 ? '+' : a < 0 ? '−' : ''}${Math.abs(Math.round(a))}°`

  return (
    <svg
      ref={svgRef}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      role="slider"
      tabIndex={0}
      aria-label="Light angle"
      aria-valuemin={-180}
      aria-valuemax={180}
      aria-valuenow={Math.round(a)}
      aria-valuetext={`${Math.round(a)} degrees, light from the ${compass(a)}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
      onDoubleClick={() => emit(DEFAULT_LIGHT_ANGLE)}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      style={{
        display: 'block',
        cursor: dragging ? 'grabbing' : 'grab',
        outline: 'none',
        touchAction: 'none',
        userSelect: 'none',
      }}
    >
      <defs>
        <radialGradient id={`face${uid}`} cx="50%" cy="45%" r="55%">
          <stop offset="0%" stopColor="#24242c" />
          <stop offset="100%" stopColor="#141419" />
        </radialGradient>
        <linearGradient id={`arc${uid}`} gradientUnits="userSpaceOnUse" x1={C} y1={C - R_TRACK} x2={sun.x} y2={sun.y}>
          <stop offset="0%" stopColor="#8f7dff" />
          <stop offset="100%" stopColor="#2fd4f0" />
        </linearGradient>
        <radialGradient id={`beam${uid}`} gradientUnits="userSpaceOnUse" cx={sun.x} cy={sun.y} r="34">
          <stop offset="0%" stopColor="#ffd98a" stopOpacity="0.32" />
          <stop offset="100%" stopColor="#ffd98a" stopOpacity="0" />
        </radialGradient>
        <linearGradient
          id={`plate${uid}`}
          gradientUnits="objectBoundingBox"
          x1={gx1 / 100}
          y1={gy1 / 100}
          x2={gx2 / 100}
          y2={gy2 / 100}
        >
          <stop offset="0%" stopColor="#6c6c82" />
          <stop offset="55%" stopColor="#363645" />
          <stop offset="100%" stopColor="#1d1d26" />
        </linearGradient>
        <linearGradient
          id={`rim${uid}`}
          gradientUnits="objectBoundingBox"
          x1={gx1 / 100}
          y1={gy1 / 100}
          x2={gx2 / 100}
          y2={gy2 / 100}
        >
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.95" />
          <stop offset="45%" stopColor="#ffffff" stopOpacity="0.08" />
          <stop offset="100%" stopColor="#ffffff" stopOpacity="0.25" />
        </linearGradient>
        <filter id={`blur${uid}`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.2" />
        </filter>
        <filter id={`glow${uid}`} x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="1.8" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <circle
        cx={C}
        cy={C}
        r={48}
        fill={`url(#face${uid})`}
        stroke="#ffffff"
        strokeOpacity={focused ? 0.0 : 0.07}
        strokeWidth={0.8}
      />
      {focused && <circle cx={C} cy={C} r={48.6} fill="none" stroke="#8f7dff" strokeOpacity={0.9} strokeWidth={1.2} />}
      {ticks}
      <circle cx={C} cy={C} r={R_TRACK} fill="none" stroke="#ffffff" strokeOpacity={0.06} strokeWidth={4.5} />
      {arc && <path d={arc} fill="none" stroke={`url(#arc${uid})`} strokeWidth={2.2} strokeLinecap="round" />}
      <circle cx={C} cy={C} r={33} fill={`url(#beam${uid})`} />

      <g transform={`translate(${C - 15} ${C - 17})`}>
        <path
          d={SQUIRCLE}
          transform={`translate(${shadowDx} ${shadowDy + 1.5})`}
          fill="#000"
          opacity={0.55}
          filter={`url(#blur${uid})`}
        />
        <path d={SQUIRCLE} fill={`url(#plate${uid})`} />
        <path d={SQUIRCLE} fill="none" stroke={`url(#rim${uid})`} strokeWidth={0.9} />
      </g>
      <text
        x={C}
        y={C + 25}
        textAnchor="middle"
        fontSize={7.5}
        fontFamily="'JetBrains Mono', 'Cascadia Code', ui-monospace, monospace"
        fill="#ececf1"
        fillOpacity={0.88}
        style={{ fontVariantNumeric: 'tabular-nums', pointerEvents: 'none' }}
      >
        {label}
      </text>

      <g transform={`translate(${sun.x} ${sun.y}) rotate(${a})`} style={{ pointerEvents: 'none' }}>
        {Array.from({ length: 8 }, (_, i) => {
          const t = (i * Math.PI) / 4
          return (
            <line
              key={i}
              x1={Math.sin(t) * 6.4}
              y1={-Math.cos(t) * 6.4}
              x2={Math.sin(t) * 8.4}
              y2={-Math.cos(t) * 8.4}
              stroke="#ffd98a"
              strokeOpacity={0.85}
              strokeWidth={0.9}
              strokeLinecap="round"
            />
          )
        })}
        <circle r={4.6} fill="#ffcf6b" filter={`url(#glow${uid})`} />
        <circle r={2.4} fill="#fff4d6" />
        {dragging && <circle r={9.8} fill="none" stroke="#ffd98a" strokeOpacity={0.35} strokeWidth={0.8} />}
      </g>
    </svg>
  )
}
