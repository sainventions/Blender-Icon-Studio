// Plate outlines in canvas space (−1..1, y up). Same formulas as the Blender worker (PLAN §2):
//   squircle = superellipse |x|^5 + |y|^5 = 1 · circle radius 1 · rounded = rounded rect with radius
//   cornerRadius × 2 (cornerRadius is a fraction of the plate size, the plate is 2 units wide) · square.
// Outlines are counter-clockwise, start on the +x axis (or the right edge) and never repeat the first point.
import type { PlateShape, Vec2 } from '../types'

export const SQUIRCLE_EXPONENT = 5

/** Default tessellation for plate outlines (arc-length uniform for curved shapes). */
export const PLATE_SEGMENTS = 256

export function plateOutline(shape: PlateShape, cornerRadius: number, segments: number = PLATE_SEGMENTS): Vec2[] {
  const n = Math.max(8, Math.round(segments))
  switch (shape) {
    case 'none':
      return []
    case 'circle':
      return circleOutline(n)
    case 'rounded':
      return roundedRectOutline(cornerRadius * 2, n)
    case 'square':
      return [
        [1, -1],
        [1, 1],
        [-1, 1],
        [-1, -1],
      ]
    case 'squircle':
    default:
      return squircleOutline(n)
  }
}

/** SVG path data for a plate outline drawn into a `size`×`size` box (SVG y-down), e.g. for 2D overlays/thumbnails. */
export function plateOutlinePath(
  shape: PlateShape,
  cornerRadius: number,
  size: number,
  inset = 0,
  segments = 128,
): string {
  const pts = plateOutline(shape === 'none' ? 'square' : shape, cornerRadius, segments)
  const half = size / 2
  const s = half - inset
  return (
    pts
      .map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${(half + x * s).toFixed(2)} ${(half - y * s).toFixed(2)}`)
      .join(' ') + ' Z'
  )
}

/** Point on the superellipse |x|^n + |y|^n = 1 for parameter t (radians). */
export function superellipsePoint(t: number, exponent = SQUIRCLE_EXPONENT): Vec2 {
  const c = Math.cos(t)
  const s = Math.sin(t)
  const p = 2 / exponent
  return [Math.sign(c) * Math.abs(c) ** p, Math.sign(s) * Math.abs(s) ** p]
}

function circleOutline(n: number): Vec2[] {
  const out: Vec2[] = []
  for (let i = 0; i < n; i++) {
    const t = (i / n) * Math.PI * 2
    out.push([Math.cos(t), Math.sin(t)])
  }
  return out
}

/** Superellipse resampled to `n` points spaced uniformly by arc length (the raw parametrisation bunches up). */
function squircleOutline(n: number): Vec2[] {
  const dense = 4096
  const xs = new Float64Array(dense + 1)
  const ys = new Float64Array(dense + 1)
  const len = new Float64Array(dense + 1)
  for (let i = 0; i <= dense; i++) {
    const [x, y] = superellipsePoint((i / dense) * Math.PI * 2)
    xs[i] = x
    ys[i] = y
    if (i > 0) len[i] = len[i - 1] + Math.hypot(x - xs[i - 1], y - ys[i - 1])
  }
  const total = len[dense]
  const out: Vec2[] = []
  let j = 1
  for (let i = 0; i < n; i++) {
    const target = (i / n) * total
    while (j < dense && len[j] < target) j++
    const seg = len[j] - len[j - 1] || 1
    const f = (target - len[j - 1]) / seg
    out.push([xs[j - 1] + (xs[j] - xs[j - 1]) * f, ys[j - 1] + (ys[j] - ys[j - 1]) * f])
  }
  return out
}

function roundedRectOutline(radius: number, n: number): Vec2[] {
  const r = Math.min(1, Math.max(0, radius))
  if (r < 1e-4) return plateOutline('square', 0)
  const perCorner = Math.max(4, Math.round(n / 4))
  const corners: [number, number, number][] = [
    [1 - r, 1 - r, 0], // top-right, sweeping 0..90°
    [-1 + r, 1 - r, 90],
    [-1 + r, -1 + r, 180],
    [1 - r, -1 + r, 270],
  ]
  const out: Vec2[] = []
  for (const [cx, cy, a0] of corners) {
    for (let k = 0; k <= perCorner; k++) {
      const a = ((a0 + (90 * k) / perCorner) * Math.PI) / 180
      const p: Vec2 = [cx + Math.cos(a) * r, cy + Math.sin(a) * r]
      const last = out[out.length - 1]
      if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) > 1e-7) out.push(p)
    }
  }
  const first = out[0]
  const last = out[out.length - 1]
  if (out.length > 1 && Math.hypot(first[0] - last[0], first[1] - last[1]) < 1e-7) out.pop()
  return out
}
