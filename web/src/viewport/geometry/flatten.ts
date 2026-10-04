// Spline JSON (svg-pipeline §7.2: cubic Bézier knots with absolute handles, holes linked to their outer contour
// via `parent`) → oriented polygon contours, ready for extrusion and cap triangulation.
import type { Spline, Vec2 } from '../../types'

export interface Contour {
  /** Flat x,y pairs (no repeated closing point). Outers are counter-clockwise, holes clockwise. */
  pts: Float64Array
  hole: boolean
}

export interface ContourGroup {
  outer: Contour
  holes: Contour[]
}

export interface FlattenOptions {
  /** Max chord deviation in art units (art square = 2.0, so 0.0008 ≈ 0.4 px at 1024 px). */
  tolerance?: number
  /** Max tangent turn per flattened segment, degrees. */
  maxTurnDeg?: number
}

const EPS = 1e-9

function isStraight(p0: Vec2, p1: Vec2, p2: Vec2, p3: Vec2): boolean {
  // Handles lying on the chord (the pipeline writes LINE segments with handles at 1/3 and 2/3).
  const dx = p3[0] - p0[0]
  const dy = p3[1] - p0[1]
  const len = Math.hypot(dx, dy)
  if (len < EPS)
    return Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < 1e-7 && Math.hypot(p2[0] - p3[0], p2[1] - p3[1]) < 1e-7
  const d1 = Math.abs((p1[0] - p0[0]) * dy - (p1[1] - p0[1]) * dx) / len
  const d2 = Math.abs((p2[0] - p0[0]) * dy - (p2[1] - p0[1]) * dx) / len
  return d1 < 1e-6 && d2 < 1e-6
}

function angleBetween(ax: number, ay: number, bx: number, by: number): number {
  const la = Math.hypot(ax, ay)
  const lb = Math.hypot(bx, by)
  if (la < EPS || lb < EPS) return 0
  const c = (ax * bx + ay * by) / (la * lb)
  return Math.acos(Math.max(-1, Math.min(1, c)))
}

/** Number of segments for a cubic: Wang's formula bounded by a max tangent turn per segment. */
function cubicSegments(p0: Vec2, p1: Vec2, p2: Vec2, p3: Vec2, tol: number, maxTurn: number): number {
  const ddx1 = p0[0] - 2 * p1[0] + p2[0]
  const ddy1 = p0[1] - 2 * p1[1] + p2[1]
  const ddx2 = p1[0] - 2 * p2[0] + p3[0]
  const ddy2 = p1[1] - 2 * p2[1] + p3[1]
  const m = Math.max(Math.hypot(ddx1, ddy1), Math.hypot(ddx2, ddy2))
  const wang = Math.ceil(Math.sqrt((0.75 * m) / tol))
  const turn =
    angleBetween(p1[0] - p0[0], p1[1] - p0[1], p2[0] - p1[0], p2[1] - p1[1]) +
    angleBetween(p2[0] - p1[0], p2[1] - p1[1], p3[0] - p2[0], p3[1] - p2[1])
  const byTurn = Math.ceil(turn / maxTurn)
  return Math.max(1, Math.min(64, Math.max(wang, byTurn)))
}

function flattenSpline(sp: Spline, tol: number, maxTurn: number): number[] {
  const P = sp.points
  const n = P.length
  const out: number[] = []
  if (n < 2) return out
  // Fill contours are always treated as closed (the pipeline emits closed contours for fills).
  for (let i = 0; i < n; i++) {
    const a = P[i]
    const b = P[(i + 1) % n]
    const p0 = a.co
    const p1 = a.hr ?? a.co
    const p2 = b.hl ?? b.co
    const p3 = b.co
    out.push(p0[0], p0[1])
    if (isStraight(p0, p1, p2, p3)) continue
    const k = cubicSegments(p0, p1, p2, p3, tol, maxTurn)
    for (let s = 1; s < k; s++) {
      const t = s / k
      const u = 1 - t
      const w0 = u * u * u
      const w1 = 3 * u * u * t
      const w2 = 3 * u * t * t
      const w3 = t * t * t
      out.push(w0 * p0[0] + w1 * p1[0] + w2 * p2[0] + w3 * p3[0], w0 * p0[1] + w1 * p1[1] + w2 * p2[1] + w3 * p3[1])
    }
  }
  return removeSmallLoops(dedupe(out))
}

/** Intersection parameter of segments p0→p1 and q0→q1 (proper crossing only), or null. */
function crossing(pts: number[], a: number, b: number, c: number, d: number): [number, number] | null {
  const ax = pts[2 * a], ay = pts[2 * a + 1], bx = pts[2 * b], by = pts[2 * b + 1]
  const cx = pts[2 * c], cy = pts[2 * c + 1], dx = pts[2 * d], dy = pts[2 * d + 1]
  const rx = bx - ax, ry = by - ay, sx = dx - cx, sy = dy - cy
  const den = rx * sy - ry * sx
  if (Math.abs(den) < 1e-18) return null
  const t = ((cx - ax) * sy - (cy - ay) * sx) / den
  const u = ((cx - ax) * ry - (cy - ay) * rx) / den
  if (t <= 1e-9 || t >= 1 - 1e-9 || u <= 1e-9 || u >= 1 - 1e-9) return null
  return [ax + t * rx, ay + t * ry]
}

/**
 * Cut tiny self-intersection loops out of a flattened ring (a crossing whose loop encloses < SMALL_LOOP of the ring's
 * area). The SVG pipeline's clip / plate-edge snap can leave such hairpin knots where art meets the plate outline
 * (Earth's waves): Blender's curve fill ignores them, but the cap triangulation (earcut) turned them into a wedge across
 * the whole layer.
 */
export function removeSmallLoops(src: number[]): number[] {
  let pts = src
  for (let pass = 0; pass < 32; pass++) {
    const n = pts.length / 2
    if (n < 5) return pts
    const total = Math.abs(signedArea(pts))
    const limit = Math.max(2e-4, SMALL_LOOP * total)
    let cut: number[] | null = null
    for (let i = 0; i < n && !cut; i++) {
      const i1 = (i + 1) % n
      for (let k = 2; k <= n >> 1 && !cut; k++) {
        const j = (i + k) % n
        const j1 = (j + 1) % n
        if (j1 === i) continue
        const hit = crossing(pts, i, i1, j, j1)
        if (!hit) continue
        // loop = i1 … j (k vertices) closed through the crossing point
        const loop: number[] = [hit[0], hit[1]]
        for (let m = 0; m < k; m++) loop.push(pts[2 * ((i1 + m) % n)], pts[2 * ((i1 + m) % n) + 1])
        if (Math.abs(signedArea(loop)) > limit) continue
        const next: number[] = [hit[0], hit[1]]
        for (let m = 0; m < n - k; m++) next.push(pts[2 * ((j1 + m) % n)], pts[2 * ((j1 + m) % n) + 1])
        cut = next
      }
    }
    if (!cut) return pts
    pts = cut
  }
  return pts
}

/** Largest loop (share of the ring's area) removeSmallLoops cuts. */
const SMALL_LOOP = 0.002

function dedupe(src: number[]): number[] {
  const out: number[] = []
  for (let i = 0; i < src.length; i += 2) {
    const x = src[i]
    const y = src[i + 1]
    const m = out.length
    if (m >= 2 && Math.abs(out[m - 2] - x) < 1e-7 && Math.abs(out[m - 1] - y) < 1e-7) continue
    out.push(x, y)
  }
  while (
    out.length >= 4 &&
    Math.abs(out[0] - out[out.length - 2]) < 1e-7 &&
    Math.abs(out[1] - out[out.length - 1]) < 1e-7
  ) {
    out.length -= 2
  }
  return out
}

export function signedArea(pts: ArrayLike<number>): number {
  let a = 0
  const n = pts.length / 2
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    a += pts[2 * i] * pts[2 * j + 1] - pts[2 * j] * pts[2 * i + 1]
  }
  return a / 2
}

function reversePairs(pts: number[]): number[] {
  const out: number[] = new Array(pts.length)
  const n = pts.length / 2
  for (let i = 0; i < n; i++) {
    out[2 * i] = pts[2 * (n - 1 - i)]
    out[2 * i + 1] = pts[2 * (n - 1 - i) + 1]
  }
  return out
}

function pointInPolygon(x: number, y: number, pts: ArrayLike<number>): boolean {
  let inside = false
  const n = pts.length / 2
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = pts[2 * i]
    const yi = pts[2 * i + 1]
    const xj = pts[2 * j]
    const yj = pts[2 * j + 1]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-30) + xi) inside = !inside
  }
  return inside
}

/** Splines → contour groups (one outer + its holes each). Degenerate contours are dropped. */
export function splinesToGroups(splines: Spline[], opts: FlattenOptions = {}): ContourGroup[] {
  const tol = opts.tolerance ?? 0.0008
  const maxTurn = ((opts.maxTurnDeg ?? 10) * Math.PI) / 180
  const flat = splines.map((sp) => {
    const pts = flattenSpline(sp, tol, maxTurn)
    const area = pts.length >= 6 ? signedArea(pts) : 0
    return { sp, pts, area }
  })

  const groups = new Map<number, ContourGroup>()
  const outers: { index: number; pts: number[]; absArea: number }[] = []
  flat.forEach((f, i) => {
    if (f.sp.hole || Math.abs(f.area) < 1e-9) return
    const pts = f.area < 0 ? reversePairs(f.pts) : f.pts
    groups.set(i, { outer: { pts: Float64Array.from(pts), hole: false }, holes: [] })
    outers.push({ index: i, pts, absArea: Math.abs(f.area) })
  })

  flat.forEach((f, i) => {
    if (!f.sp.hole || Math.abs(f.area) < 1e-9) return
    const pts = f.area > 0 ? reversePairs(f.pts) : f.pts
    let g = groups.get(f.sp.parent)
    if (!g) {
      // Missing/invalid parent: attach to the smallest outer that contains the hole's first point.
      let best: { index: number; absArea: number } | null = null
      for (const o of outers) {
        if (
          o.absArea > Math.abs(f.area) &&
          pointInPolygon(pts[0], pts[1], o.pts) &&
          (!best || o.absArea < best.absArea)
        )
          best = o
      }
      if (best) g = groups.get(best.index)
    }
    if (g) g.holes.push({ pts: Float64Array.from(pts), hole: true })
  })

  return [...groups.values()]
}

/** Polygon (e.g. a plate outline) → a single contour group. */
export function polygonToGroup(points: Vec2[]): ContourGroup | null {
  const flatPts: number[] = []
  for (const [x, y] of points) flatPts.push(x, y)
  const pts = dedupe(flatPts)
  if (pts.length < 6) return null
  const area = signedArea(pts)
  if (Math.abs(area) < 1e-9) return null
  return { outer: { pts: Float64Array.from(area < 0 ? reversePairs(pts) : pts), hole: false }, holes: [] }
}

/** Axis-aligned bounds of contour groups: [minx, miny, maxx, maxy]. */
export function groupsBounds(groups: ContourGroup[]): [number, number, number, number] {
  let minx = Infinity
  let miny = Infinity
  let maxx = -Infinity
  let maxy = -Infinity
  for (const g of groups) {
    const p = g.outer.pts
    for (let i = 0; i < p.length; i += 2) {
      if (p[i] < minx) minx = p[i]
      if (p[i] > maxx) maxx = p[i]
      if (p[i + 1] < miny) miny = p[i + 1]
      if (p[i + 1] > maxy) maxy = p[i + 1]
    }
  }
  return [minx, miny, maxx, maxy]
}
