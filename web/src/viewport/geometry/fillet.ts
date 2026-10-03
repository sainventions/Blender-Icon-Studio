// Spline hygiene + corner fillets — a port of blender_worker/geometry.py (sanitize, merge_collinear, max_turn_deg,
// fillet_corners) so the three.js pill bodies get the same silhouettes as Blender's curve route (PLAN D3):
//   · region / silhouette splines are fills: an open spline (`closed: false`, an SVG subpath without 'Z') is closed
//     with a straight segment, as SVG fills it (the worker used to sweep it as a hollow tube);
//   · coincident consecutive knots are merged (zero-length segments make the round bevel spike);
//   · runs of almost collinear straight segments are joined (boolean-op outlines split edges into short pieces, which
//     limited the fillet of the corner between them);
//   · every CONVEX corner sharper than 22° is rounded with a circular fillet of radius 1.2 × bevel (the inset outline
//     of a tighter corner would loop); concave corners stay sharp (a fillet there grows the silhouette); a fillet that
//     does not fit in 45 % of the adjacent segments leaves the corner sharp;
//   · pieces with a cusp (> 150° turn) or an unfilletable convex corner sharper than 100° are not filleted (the worker
//     routes them to the GN mesh fallback, whose angle-limited bevel keeps every corner sharp).
// Unlike the worker, degenerate splines are kept as empty placeholders so `Spline.parent` indices stay valid.
import type { Spline, SplinePoint, Vec2 } from '../../types'

export const MIN_BEVEL = 1e-4
export const FILLET_MIN_TURN = 22
export const FILLET_RATIO = 1.2
export const CUSP_DEG = 150
/** Worker ACUTE_GN_DEG: an unfilletable convex corner sharper than this sends the piece to the GN (unfilleted) route. */
export const ACUTE_GN_DEG = 100
/** Worker merge_collinear: max turn (degrees) at a removable joint, max distance of a removed knot from the chord. */
export const STRAIGHT_TURN = 3
export const STRAIGHT_TOL = 4e-4

type Seg = [Vec2, Vec2, Vec2, Vec2]

const d2 = (a: Vec2, b: Vec2) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

function bez(s: Seg, t: number): Vec2 {
  const mt = 1 - t
  const a = mt * mt * mt
  const b = 3 * mt * mt * t
  const c = 3 * mt * t * t
  const d = t * t * t
  return [a * s[0][0] + b * s[1][0] + c * s[2][0] + d * s[3][0], a * s[0][1] + b * s[1][1] + c * s[2][1] + d * s[3][1]]
}

function flatArea(pts: SplinePoint[], n = 6): number {
  const ring: Vec2[] = []
  const m = pts.length
  for (let i = 0; i < m; i++) {
    const a = pts[i]
    const b = pts[(i + 1) % m]
    const seg: Seg = [a.co, a.hr ?? a.co, b.hl ?? b.co, b.co]
    for (let j = 0; j < n; j++) ring.push(bez(seg, j / n))
  }
  let s = 0
  for (let i = 0; i < ring.length; i++) {
    const p = ring[i]
    const q = ring[(i + 1) % ring.length]
    s += p[0] * q[1] - q[0] * p[1]
  }
  return s / 2
}

const third = (a: Vec2, b: Vec2): Vec2 => [a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3]

/**
 * Fills are always closed: an open spline gets a straight closing segment (its dangling end handles are meaningless).
 * Coincident consecutive knots are merged; degenerate slivers become empty placeholders (index-preserving).
 */
export function sanitizeSplines(splines: Spline[], eps = 2.5e-4, minArea = 2e-7): Spline[] {
  const e2 = eps * eps
  return splines.map((s) => {
    const merged: SplinePoint[] = []
    for (const p of s.points ?? []) {
      const q: SplinePoint = { co: p.co, hl: p.hl ?? p.co, hr: p.hr ?? p.co }
      const last = merged[merged.length - 1]
      if (last && d2(q.co, last.co) < e2) merged[merged.length - 1] = { ...last, hr: q.hr }
      else merged.push(q)
    }
    if (merged.length > 1 && d2(merged[0].co, merged[merged.length - 1].co) < e2) {
      const last = merged.pop()!
      merged[0] = { ...merged[0], hl: last.hl }
    } else if (s.closed === false && merged.length > 1) {
      const a = merged[merged.length - 1]
      const b = merged[0]
      merged[merged.length - 1] = { ...a, hr: third(a.co, b.co) }
      merged[0] = { ...b, hl: third(b.co, a.co) }
    }
    if (merged.length < 2 || Math.abs(flatArea(merged)) < minArea) return { ...s, closed: true, points: [] }
    return { ...s, closed: true, points: merged }
  })
}

function segStraight(p0: Vec2, c1: Vec2, c2: Vec2, p1: Vec2, tol: number): boolean {
  const dx = p1[0] - p0[0]
  const dy = p1[1] - p0[1]
  const ln = Math.hypot(dx, dy)
  if (ln < 1e-9) return true
  for (const c of [c1, c2]) {
    const rx = c[0] - p0[0]
    const ry = c[1] - p0[1]
    if (Math.abs(rx * dy - ry * dx) / ln > tol) return false
    const t = (rx * dx + ry * dy) / ln
    if (t < -tol || t > ln + tol) return false
  }
  return true
}

function ptLineDist(p: Vec2, a: Vec2, b: Vec2): number {
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const l2 = dx * dx + dy * dy
  if (l2 < 1e-24) return Math.hypot(p[0] - a[0], p[1] - a[1])
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)
}

/**
 * Worker merge_collinear: remove knots joining two straight segments almost in line (turn < `maxTurn`) as long as
 * every removed knot stays within `tol` of the merged chord. Curved segments are kept.
 */
export function mergeCollinear(splines: Spline[], maxTurn = STRAIGHT_TURN, tol = STRAIGHT_TOL): Spline[] {
  const cosT = Math.cos((maxTurn * Math.PI) / 180)
  return splines.map((s) => {
    const pts = s.points
    const m = pts.length
    if (m < 4) return s
    const straight = pts.map((p, i) => segStraight(p.co, p.hr, pts[(i + 1) % m].hl, pts[(i + 1) % m].co, tol))
    const removable = (i: number): boolean => {
      if (!(straight[(i - 1 + m) % m] && straight[i])) return false
      const a = pts[(i - 1 + m) % m].co
      const p = pts[i].co
      const b = pts[(i + 1) % m].co
      const u = unit([p[0] - a[0], p[1] - a[1]])
      const v = unit([b[0] - p[0], b[1] - p[1]])
      return u[0] * v[0] + u[1] * v[1] >= cosT
    }
    const rem = pts.map((_, i) => removable(i))
    if (!rem.some(Boolean)) return s
    let start = rem.findIndex((r) => !r)
    if (start < 0) start = 0
    const keep = [start]
    let run: number[] = []
    for (let k = 1; k <= m; k++) {
      const i = (start + k) % m
      if (k < m && rem[i]) {
        const a = pts[keep[keep.length - 1]].co
        const b = pts[(i + 1) % m].co
        if ([...run, i].every((j) => ptLineDist(pts[j].co, a, b) <= tol)) {
          run.push(i)
          continue
        }
        keep.push(i) // deviation too large: keep this knot as a new anchor
        run = []
        continue
      }
      if (k < m) keep.push(i)
      run = []
    }
    if (keep.length < 3 || keep.length === m) return s
    const out: SplinePoint[] = keep.map((i) => ({ co: pts[i].co, hl: pts[i].hl, hr: pts[i].hr }))
    const n = out.length
    for (let k = 0; k < n; k++) {
      const i = keep[k]
      const j = keep[(k + 1) % n]
      if ((((j - i) % m) + m) % m !== 1) {
        // merged run: a straight chord with handles at thirds
        const a = out[k]
        const b = out[(k + 1) % n]
        a.hr = third(a.co, b.co)
        b.hl = third(b.co, a.co)
      }
    }
    return { ...s, points: out }
  })
}

/** Worker _material_left: the filled side is left of the travel direction (CCW outer or CW hole / odd depth). */
function materialLeft(s: Spline): boolean {
  const area = flatArea(s.points, 4)
  if (area === 0) return true
  const hole = !!s.hole || (Number(s.depth) || 0) % 2 === 1
  return area > 0 !== hole
}

/** Largest tangent turn at any knot, degrees (180 = cusp). */
export function maxTurnDeg(splines: Spline[]): number {
  let best = 0
  for (const s of splines) {
    const pts = s.points
    const m = pts.length
    if (m < 2) continue
    for (let i = 0; i < m; i++) {
      const p = pts[i]
      const co = p.co
      let ti: Vec2 = [co[0] - p.hl[0], co[1] - p.hl[1]]
      if (ti[0] * ti[0] + ti[1] * ti[1] < 1e-14) {
        const q = pts[(i - 1 + m) % m]
        ti = [co[0] - q.hr[0], co[1] - q.hr[1]]
        if (ti[0] * ti[0] + ti[1] * ti[1] < 1e-14) ti = [co[0] - q.co[0], co[1] - q.co[1]]
      }
      let to: Vec2 = [p.hr[0] - co[0], p.hr[1] - co[1]]
      if (to[0] * to[0] + to[1] * to[1] < 1e-14) {
        const q = pts[(i + 1) % m]
        to = [q.hl[0] - co[0], q.hl[1] - co[1]]
        if (to[0] * to[0] + to[1] * to[1] < 1e-14) to = [q.co[0] - co[0], q.co[1] - co[1]]
      }
      const a = Math.abs((Math.atan2(ti[0] * to[1] - ti[1] * to[0], ti[0] * to[0] + ti[1] * to[1]) * 180) / Math.PI)
      if (a > best) best = a
    }
  }
  return best
}

/** Control points of `seg` restricted to [t0, t1] (two de Casteljau splits). */
function subSeg(seg: Seg, t0: number, t1: number): Seg {
  const split = (s: Seg, t: number): [Seg, Seg] => {
    const lerp = (a: Vec2, b: Vec2): Vec2 => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
    const a = lerp(s[0], s[1])
    const b = lerp(s[1], s[2])
    const c = lerp(s[2], s[3])
    const d = lerp(a, b)
    const e = lerp(b, c)
    const f = lerp(d, e)
    return [
      [f, e, c, s[3]],
      [s[0], a, d, f],
    ]
  }
  let s = seg
  let u1 = t1
  if (t0 > 1e-9) {
    s = split(s, t0)[0]
    u1 = t0 < 1 ? (t1 - t0) / (1 - t0) : 1
  }
  if (u1 < 1 - 1e-9) s = split(s, u1)[1]
  return s
}

function arclenTable(seg: Seg, n = 16): number[] {
  const acc = [0]
  let prev = bez(seg, 0)
  for (let i = 1; i <= n; i++) {
    const p = bez(seg, i / n)
    acc.push(acc[i - 1] + Math.hypot(p[0] - prev[0], p[1] - prev[1]))
    prev = p
  }
  return acc
}

function tAt(acc: number[], length: number, fromEnd = false): number {
  const total = acc[acc.length - 1]
  if (total <= 1e-12) return fromEnd ? 1 : 0
  const target = Math.max(0, Math.min(total, fromEnd ? total - length : length))
  const n = acc.length - 1
  for (let i = 0; i < n; i++) {
    if (acc[i + 1] >= target) {
      const seg = acc[i + 1] - acc[i]
      const f = seg <= 1e-15 ? 0 : (target - acc[i]) / seg
      return (i + f) / n
    }
  }
  return 1
}

function unit(v: Vec2): Vec2 {
  const n = Math.hypot(v[0], v[1])
  return n > 1e-12 ? [v[0] / n, v[1] / n] : [0, 0]
}

const isZero = (v: Vec2) => v[0] === 0 && v[1] === 0

/**
 * Round every corner sharper than `minTurn` with an (approximately circular) fillet of `radius`. A fillet that does
 * not fit in 45 % of the adjacent segments is shrunk, or — when `minRadius` > 0 — skipped (the corner stays sharp; its
 * turn angle is appended to `skipped`). `convexOnly`: concave corners stay sharp (a fillet there adds material outside
 * the outline, and the inset outline of a concave corner does not loop anyway).
 */
export function filletCorners(
  splines: Spline[],
  radius: number,
  minTurn = FILLET_MIN_TURN,
  minRadius = 0,
  convexOnly = true,
  skipped?: number[],
): Spline[] {
  if (radius <= 1e-6) return splines
  return splines.map((s) => {
    const pts = s.points
    const m = pts.length
    if (s.closed === false || m < 2) return s
    const matLeft = convexOnly ? materialLeft(s) : true
    const segs: Seg[] = pts.map((p, i) => [p.co, p.hr, pts[(i + 1) % m].hl, pts[(i + 1) % m].co])
    const acc = segs.map((sg) => arclenTable(sg))
    const cut = new Array<number>(m).fill(0)
    const turn = new Array<number>(m).fill(0)
    for (let i = 0; i < m; i++) {
      const sin = segs[(i - 1 + m) % m]
      const sout = segs[i]
      let tin = unit([sin[3][0] - sin[2][0], sin[3][1] - sin[2][1]])
      if (isZero(tin)) tin = unit([sin[3][0] - sin[1][0], sin[3][1] - sin[1][1]])
      if (isZero(tin)) tin = unit([sin[3][0] - sin[0][0], sin[3][1] - sin[0][1]])
      let tout = unit([sout[1][0] - sout[0][0], sout[1][1] - sout[0][1]])
      if (isZero(tout)) tout = unit([sout[2][0] - sout[0][0], sout[2][1] - sout[0][1]])
      if (isZero(tout)) tout = unit([sout[3][0] - sout[0][0], sout[3][1] - sout[0][1]])
      const ang = (Math.acos(Math.max(-1, Math.min(1, tin[0] * tout[0] + tin[1] * tout[1]))) * 180) / Math.PI
      if (convexOnly && tin[0] * tout[1] - tin[1] * tout[0] > 0 !== matLeft) continue // concave corner
      if (ang > minTurn && ang < 175) {
        turn[i] = ang
        cut[i] = radius * Math.tan((ang * Math.PI) / 360)
      }
    }
    if (!cut.some((c) => c > 0)) return s
    for (let i = 0; i < m; i++) {
      if (!cut[i]) continue
      const lenIn = acc[(i - 1 + m) % m]
      const lim = 0.45 * Math.min(lenIn[lenIn.length - 1], acc[i][acc[i].length - 1])
      if (cut[i] > lim) {
        if (minRadius > 0) skipped?.push(turn[i])
        cut[i] = minRadius > 0 ? 0 : lim
      }
    }

    interface Knot {
      co: Vec2
      hl: Vec2 | null
      hr: Vec2 | null
      cornerOut?: number
    }
    const knots: Knot[] = []
    for (let i = 0; i < m; i++) {
      const j = (i + 1) % m
      let t0 = cut[i] ? tAt(acc[i], cut[i]) : 0
      let t1 = cut[j] ? tAt(acc[i], cut[j], true) : 1
      if (t1 <= t0 + 1e-6) t0 = t1 = (t0 + t1) / 2
      const [p0, c1, c2, p1] = subSeg(segs[i], t0, t1)
      knots.push({ co: p0, hl: null, hr: c1 })
      knots.push({ co: p1, hl: c2, hr: null, cornerOut: j })
    }
    const stitched: Knot[] = []
    const n2 = knots.length
    for (let k = 0; k < n2; k += 2) {
      const start = knots[k]
      const end = knots[k + 1]
      const next = knots[(k + 2) % n2]
      const j = end.cornerOut!
      stitched.push(start)
      if (cut[j] > 0) {
        const corner = segs[j][0]
        const phi = (turn[j] * Math.PI) / 180
        const rEff = cut[j] / Math.max(1e-9, Math.tan(phi / 2))
        const h = (4 / 3) * Math.tan(phi / 4) * rEff
        const dEnd = unit([corner[0] - end.co[0], corner[1] - end.co[1]])
        const dNext = unit([corner[0] - next.co[0], corner[1] - next.co[1]])
        end.hr = [end.co[0] + dEnd[0] * h, end.co[1] + dEnd[1] * h]
        next.hl = [next.co[0] + dNext[0] * h, next.co[1] + dNext[1] * h]
        stitched.push(end)
      } else {
        // No fillet: the segment end and the next start coincide → one knot.
        next.hl = end.hl
      }
    }
    return {
      ...s,
      points: stitched.map((p) => ({ co: p.co, hl: p.hl ?? p.co, hr: p.hr ?? p.co })),
    }
  })
}

/**
 * The worker's curve-route preparation for one piece (curve_data): sanitize (always-closed fills), join collinear runs,
 * then fillet the convex corners — unless the piece has a cusp or an unfilletable acute convex corner (GN route: no
 * fillets).
 */
export function prepareSplines(splines: Spline[], bevel: number): Spline[] {
  const clean = mergeCollinear(sanitizeSplines(splines))
  if (bevel <= MIN_BEVEL || maxTurnDeg(clean) > CUSP_DEG) return clean
  const skipped: number[] = []
  const shaped = sanitizeSplines(filletCorners(clean, FILLET_RATIO * bevel, FILLET_MIN_TURN, bevel, true, skipped))
  return skipped.some((t) => t > ACUTE_GN_DEG) ? clean : shaped
}
