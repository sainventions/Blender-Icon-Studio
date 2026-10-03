// Colour helpers (sRGB hex ⇄ rgb, luminance, interpolation) and fill → CSS conversion.
import type { Fill, GradientStop, Paint, Vec2 } from '../types'

export type RGB = [number, number, number]

export function normalizeHex(input: string): string | null {
  let s = input.trim().replace(/^#/, '').toLowerCase()
  if (/^[0-9a-f]{3}$/.test(s)) s = s.split('').map((c) => c + c).join('')
  if (/^[0-9a-f]{8}$/.test(s)) s = s.slice(0, 6)
  if (!/^[0-9a-f]{6}$/.test(s)) return null
  return `#${s}`
}

export function hexToRgb(hex: string): RGB {
  const h = normalizeHex(hex) ?? '#000000'
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]
}

export function rgbToHex([r, g, b]: RGB): string {
  const c = (v: number) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')
  return `#${c(r)}${c(g)}${c(b)}`
}

export function rgba(hex: string, alpha = 1): string {
  const [r, g, b] = hexToRgb(hex)
  return alpha >= 1 ? `rgb(${r} ${g} ${b})` : `rgb(${r} ${g} ${b} / ${Math.max(0, alpha).toFixed(3)})`
}

export function mixHex(a: string, b: string, t: number): string {
  const ca = hexToRgb(a)
  const cb = hexToRgb(b)
  return rgbToHex([ca[0] + (cb[0] - ca[0]) * t, ca[1] + (cb[1] - ca[1]) * t, ca[2] + (cb[2] - ca[2]) * t])
}

/** Relative luminance (WCAG) 0..1. */
export function luminance(hex: string): number {
  const lin = (v: number) => {
    const c = v / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  const [r, g, b] = hexToRgb(hex)
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

export function readableOn(hex: string): string {
  return luminance(hex) > 0.45 ? '#111114' : '#ffffff'
}

/** Colour of a gradient at offset t (stops assumed sorted). */
export function sampleStops(stops: GradientStop[], t: number): { color: string; opacity: number } {
  if (!stops.length) return { color: '#ffffff', opacity: 1 }
  const sorted = [...stops].sort((a, b) => a.offset - b.offset)
  if (t <= sorted[0].offset) return { color: sorted[0].color, opacity: sorted[0].opacity }
  const last = sorted[sorted.length - 1]
  if (t >= last.offset) return { color: last.color, opacity: last.opacity }
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i]
    const b = sorted[i + 1]
    if (t >= a.offset && t <= b.offset) {
      const k = b.offset === a.offset ? 0 : (t - a.offset) / (b.offset - a.offset)
      return { color: mixHex(a.color, b.color, k), opacity: a.opacity + (b.opacity - a.opacity) * k }
    }
  }
  return { color: last.color, opacity: last.opacity }
}

export function stopsToCss(stops: GradientStop[]): string {
  const sorted = [...stops].sort((a, b) => a.offset - b.offset)
  if (sorted.length === 1) sorted.push({ ...sorted[0], offset: 1 })
  return sorted.map((s) => `${rgba(s.color, s.opacity)} ${(s.offset * 100).toFixed(1)}%`).join(', ')
}

/**
 * Angle (CSS convention, degrees: 0 = bottom→top, 90 = left→right, 180 = top→bottom) of a linear gradient
 * whose start/end points are in ART space (y up).
 */
export function linearAngle(start: Vec2, end: Vec2): number {
  const dx = end[0] - start[0]
  const dy = end[1] - start[1]
  if (dx === 0 && dy === 0) return 180
  // art y up: direction (sin θ, cos θ)
  const deg = (Math.atan2(dx, dy) * 180) / Math.PI
  return (deg + 360) % 360
}

/** Start/end points (art space) for a CSS-style angle, centred on `center`, half-length `r`. */
export function linearPointsFromAngle(angleDeg: number, center: Vec2 = [0, 0], r = 1): { start: Vec2; end: Vec2 } {
  const a = (angleDeg * Math.PI) / 180
  const dx = Math.sin(a) * r
  const dy = Math.cos(a) * r
  const round = (v: number) => Math.round(v * 10000) / 10000
  return {
    start: [round(center[0] - dx), round(center[1] - dy)],
    end: [round(center[0] + dx), round(center[1] + dy)],
  }
}

export const SYSTEM_FILLS = {
  'system-light': ['#ffffff', '#e4e5ea'],
  'system-dark': ['#3a3a3f', '#111114'],
} as const

/** CSS background for a fill (used in chips, swatches and previews). */
export function fillToCss(fill: Fill | Paint | null | undefined, fallback = 'transparent'): string {
  if (!fill) return fallback
  switch (fill.type) {
    case 'auto':
      return 'conic-gradient(from 200deg, #ff5f6d, #ffc371, #47e891, #2fd4f0, #8f7dff, #ff5f6d)'
    case 'none':
      return 'transparent'
    case 'solid':
      return rgba(fill.color, fill.opacity)
    case 'linear':
      return `linear-gradient(${linearAngle(fill.start, fill.end).toFixed(1)}deg, ${stopsToCss(fill.stops)})`
    case 'radial':
      return `radial-gradient(circle at ${(((fill.center?.[0] ?? 0) + 1) / 2) * 100}% ${
        (1 - ((fill.center?.[1] ?? 0) + 1) / 2) * 100
      }%, ${stopsToCss(fill.stops)})`
    case 'system-light':
      return `linear-gradient(180deg, ${SYSTEM_FILLS['system-light'][0]}, ${SYSTEM_FILLS['system-light'][1]})`
    case 'system-dark':
      return `linear-gradient(180deg, ${SYSTEM_FILLS['system-dark'][0]}, ${SYSTEM_FILLS['system-dark'][1]})`
  }
}

/** Representative flat colour of a paint (for compact chips / sorting). */
export function paintColor(fill: Fill | Paint | null | undefined): string | null {
  if (!fill) return null
  switch (fill.type) {
    case 'solid':
      return fill.color
    case 'linear':
    case 'radial':
      return sampleStops(fill.stops, 0.5).color
    case 'system-light':
      return '#f2f2f5'
    case 'system-dark':
      return '#26262a'
    default:
      return null
  }
}

/** Apple system palette for quick picks. */
export const SYSTEM_SWATCHES = [
  '#ffffff', '#e4e5ea', '#8e8e93', '#3a3a3f', '#111114', '#000000',
  '#ff3b30', '#ff9500', '#ffcc00', '#34c759', '#00c7be', '#30b0c7',
  '#32ade6', '#007aff', '#5856d6', '#af52de', '#ff2d55', '#a2845e',
]
