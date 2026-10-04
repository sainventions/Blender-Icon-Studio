// Height-field bodies (PLAN §11 Geometry) — the three.js twin of blender_worker/heightfield.py, used for EVERY piece,
// silhouette, raster contour, card and the plate.
//
// A body is a watertight, smooth solid over a 2D outline: a round edge that only depends on the inward distance d to
// that outline (thin parts simply taper, tips / corners never fold over or self-intersect) plus — with inflate — a
// POISSON dome (PLAN §11 round 7): −∇²u = 4 on the triangulation, u = 0 on the outline, dome = inflate · D_P ·
// √(u / max_P u) per connected part P (a disc becomes a hemisphere-like dome, thin parts round tapering tubes, no
// medial-axis creases / fins). The profile, the sampling (graded offset rings along inward bisectors + medial points
// + the island apex; inflated bodies: per-ray dome ROWS scaled to the local width), the chord passes, the Poisson
// solve (cotangent Laplacian, Jacobi-preconditioned conjugate gradients on typed arrays), the normals (softmin over
// FOOT POINTS + the dome's least-squares quadratic gradient, safe blend toward +Z, singular rim splits, creased
// flat-slab walls) and the assembly follow heightfield.py step by step (see its module docstring, mirrored constants
// below). The only difference is the triangulator: poly2tri (one constrained Delaunay triangulation per island: outer
// ring + its holes + Steiner points, then Lawson flips to the Delaunay one) instead of mathutils' delaunay_2d_cdt.
// The pieces of ONE layer meet like in scene.py (_relations): touching ones pull back INSET_GAP from each other,
// overlapping ones stack by their real heights (ringsRelation / insetRings / stackShifts below).
// Round 8 (mirrored): a minimum vertical wall (rimBevel, WALL_MIN), the round-edge radius capped at each part's LOCAL
// half-width (localWidth / blendWidth / rimRadius: thin strokes are round tubes, small discs spheres, no roof ridge),
// medial points bisected onto the spine, and a deterministic dome apex (islandInradius).
//
// Units: everything is in the piece's LOCAL units (art units of its splines); tolerances are WORLD units divided by
// `scale` (art scale × layer scale), exactly like the worker. The body is centred on its mid-plane z = 0.
import './globalShim.ts'
import * as poly2triNs from 'poly2tri'
import type { Spline } from '../../types'

// CJS interop: named exports (Node, Vite pre-bundle) or only a default export (some bundlers)
const poly2tri: typeof poly2triNs =
  (poly2triNs as { SweepContext?: unknown }).SweepContext ? poly2triNs : (poly2triNs as unknown as { default: typeof poly2triNs }).default

// ------------------------------------------------------------------------------------------------ tunables (WORLD units)
export const CHORD_TOL = 0.0006
export const MAX_EDGE = 0.04
export const MERGE_EPS = 1e-5
export const MERGE_Q = 2e-6
export const MIN_AREA = 1e-8
export const CORNER_DEG = 30.0
export const GUARD_MIN = 0.002
export const GUARD_MAX = 0.008
export const FAN_DEG = 15.0
export const RING_TOL = 0.04
export const KAPPA = 0.0015
export const KAPPA_REL = 0.3
export const SOFT_REACH = 4.0
export const NEAR = 0.01
export const TAN_K = 0.006
export const TAN_MIN = 0.004
export const TAN_MAX = 0.04
export const NORMAL_MIN_DOT = 0.05
export const APEX_CLEAR = 0.3
export const MIN_THICKNESS = 1e-4
export const CHORD_PASSES = 4
/** Inflated bodies: bisection steps of each ray's medial distance (dome rows, SAMPLING 3b). */
export const BISECT = 4
/** −∇²u = POISSON_F: a disc of radius R gets u = R² − r² (u_max = R²). */
export const POISSON_F = 4.0
/** Conjugate-gradient stop: |residual| ≤ POISSON_TOL × |load|. */
export const POISSON_TOL = 1e-5
export const POISSON_MAX_ITER = 4000
/** The round edge keeps at least this fraction of the half thickness as a vertical wall (round 8, rimBevel). */
export const WALL_MIN = 0.15
/** Bisection steps of the local half-width at every outline vertex (localWidth, SAMPLING 3c). */
export const WIDTH_BISECT = 10
/** Its absolute slack × CHORD_TOL: a disc tangent to the TRUE curve pokes out of the sampled outline. */
export const WIDTH_TOL = 2.0
/** Foot points of a vertex blend their local half-widths over this × its distance (blendWidth). */
export const WIDTH_BLEND = 0.5
/** × the inradius grid cell: distances this close tie (deterministic apex, SAMPLING 2). */
export const APEX_TIE = 1e-4

type Ring = Float64Array // flat x,y pairs, no repeated closing point

// ================================================================================================ profile
/** The round-edge radius a body really gets: b = min(bevel, (1 − WALL_MIN)·t/2) — a minimum vertical wall of WALL_MIN ×
 *  the half thickness (round 8: knife-thin rims made tube ends refract the studio's dark side — Gemini's tip notch). */
export function rimBevel(thickness: number, bevel: number): number {
  return Math.max(0, Math.min(bevel, ((1 - WALL_MIN) * thickness) / 2))
}

/** e = max(t/2 − b, 0) with b = rimBevel (≥ WALL_MIN·t/2): half height of the vertical side wall. */
export function wallHalf(thickness: number, bevel: number): number {
  return Math.max(thickness / 2 - rimBevel(thickness, bevel), 0)
}

/** hb(d; β) = sqrt(β² − (β − min(d, β))²): the round edge, a quarter circle of radius β (flat beyond d = β). */
export function rimHeight(d: number, beta: number): number {
  const dd = Math.max(d, 0)
  const bb = Math.max(beta, 0)
  return Math.sqrt(Math.max(bb * bb - (bb - Math.min(dd, bb)) ** 2, 0))
}

/** (∂hb/∂d, ∂hb/∂β) of rimHeight: ((β − d)/hb, d/hb) for d < β (∂/∂d infinite at d = 0), else (0, 1). */
export function rimSlopes(d: number, beta: number): [number, number] {
  const dd = Math.max(d, 0)
  const bb = Math.max(beta, 0)
  if (!(dd < bb)) return [0, 1]
  const hb = rimHeight(dd, bb)
  return hb > 0 ? [(bb - dd) / hb, dd / hb] : [Infinity, 0]
}

/** Top height z(d) = e + hb(d; min(b, D)) + inflate·D·sqrt(1 − (1 − min(d/D, 1))²) (mirrored for the bottom): the DISC
 *  MODEL of an island of inradius D — its local half-width is D everywhere, so the round edge is capped at D (round 8: a
 *  disc narrower than the bevel is a sphere) — plus the disc model of the Poisson dome (a body itself uses its solved u
 *  and the local half-width — buildBody). Used for sampling and the half height. b = rimBevel(thickness, bevel). */
export function profile(d: number, thickness: number, bevel: number, inflate: number, D: number): number {
  const dd = Math.max(d, 0)
  const b = rimBevel(thickness, bevel)
  let z = wallHalf(thickness, b)
  if (b > 0) z += rimHeight(dd, Math.min(b, Math.max(D, 1e-12)))
  const k = Math.max(0, inflate)
  if (k > 0) {
    const Dv = Math.max(D, 1e-12)
    const u = Math.min(dd / Dv, 1)
    z += k * Dv * Math.sqrt(Math.max(1 - (1 - u) ** 2, 0))
  }
  return z
}

/** dz/dd of profile with the round edge capped at D (Infinity at d = 0 when bevel > 0 or inflate > 0). `bevel` is used
 *  as given (the builder passes the rimBevel'd radius). */
export function slope(d: number, bevel: number, inflate: number, D: number): number {
  const dd = Math.max(d, 0)
  const b = Math.max(0, bevel)
  let s = 0
  if (b > 0) s += rimSlopes(dd, Math.min(b, Math.max(D, 1e-12)))[0]
  const k = Math.max(0, inflate)
  if (k > 0) {
    const Dv = Math.max(D, 1e-12)
    const u = Math.min(dd / Dv, 1)
    if (u < 1) {
      const r = Math.sqrt(Math.max(1 - (1 - u) ** 2, 0))
      s += r > 0 ? (k * (1 - u)) / r : Infinity
    }
  }
  return s
}

/** Max top height of an island with inradius D (= its half height): max(t/2 − b, 0) + min(b, D) + inflate·D with
 *  b = rimBevel(t, bevel) (the local bevel cap: at the island's apex the local half-width is D). */
export function halfHeight(thickness: number, bevel: number, inflate: number, D: number): number {
  return profile(Math.max(D, 0), thickness, bevel, inflate, Math.max(D, 1e-12))
}

/** Inward distances of the graded offset rings of one island (sorted, 0 < d < D). */
export function ringDistances(bevel: number, inflate: number, D: number, segments: number): number[] {
  const out: number[] = []
  const b = Math.max(0, bevel)
  if (b > 0) {
    const K = Math.max(2, Math.min(16, Math.trunc(segments)))
    for (let j = 1; j <= K; j++) out.push(b * (1 - Math.cos((j * Math.PI) / (2 * K))))
  }
  if (inflate > 0 && D > 0) {
    const J = Math.max(3, Math.min(12, Math.trunc(segments)))
    for (let j = 1; j < J; j++) out.push(D * (1 - Math.cos((j * Math.PI) / (2 * J))))
  }
  let ds = out.filter((x) => x > 0 && x < D * 0.999).sort((a, c) => a - c)
  if (ds.length < 2) return ds
  const keep = [ds[0]]
  for (const x of ds.slice(1)) if (x - keep[keep.length - 1] > 1e-12) keep.push(x)
  ds = keep
  // merge rings much closer than their neighbours' gaps (bevel and dome rings interleave)
  while (ds.length > 2) {
    const gaps = ds.map((x, i) => x - (i ? ds[i - 1] : 0))
    let bad = -1
    for (let i = 1; i < gaps.length; i++) {
      const nb = Math.min(i + 1 < gaps.length ? gaps[i + 1] : Infinity, gaps[i - 1])
      if (gaps[i] < 0.25 * nb) {
        bad = i
        break
      }
    }
    if (bad < 0) break
    ds = ds.filter((_, i) => i !== bad)
  }
  return ds
}

// ================================================================================================ outline
/** Adaptive polyline of one bezier spline (closed ring, no repeated end point). maxEdge 0: chord tolerance only. */
export function flattenSpline(s: Spline, tol: number, maxEdge: number): number[] {
  const P = s.points ?? []
  const m = P.length
  const out: number[] = []
  if (m < 2) {
    for (const p of P) out.push(p.co[0], p.co[1])
    return out
  }
  const closed = s.closed ?? true
  const nseg = closed ? m : m - 1
  for (let i = 0; i < nseg; i++) {
    const a = P[i]
    const b = P[(i + 1) % m]
    const p0 = a.co
    const c1 = a.hr ?? a.co
    const c2 = b.hl ?? b.co
    const p1 = b.co
    const L = Math.max(
      Math.hypot(p0[0] - 2 * c1[0] + c2[0], p0[1] - 2 * c1[1] + c2[1]),
      Math.hypot(c1[0] - 2 * c2[0] + p1[0], c1[1] - 2 * c2[1] + p1[1]),
    )
    const nCurv = Math.ceil(Math.sqrt((0.75 * L) / Math.max(tol, 1e-12)))
    const plen =
      Math.hypot(c1[0] - p0[0], c1[1] - p0[1]) + Math.hypot(c2[0] - c1[0], c2[1] - c1[1]) + Math.hypot(p1[0] - c2[0], p1[1] - c2[1])
    const nLen = maxEdge ? Math.ceil(plen / Math.max(maxEdge, 1e-12)) : 1
    const n = Math.max(1, Math.min(256, Math.max(nCurv, nLen)))
    for (let k = 0; k < n; k++) {
      const t = k / n
      const u = 1 - t
      const w0 = u * u * u
      const w1 = 3 * u * u * t
      const w2 = 3 * u * t * t
      const w3 = t * t * t
      out.push(w0 * p0[0] + w1 * c1[0] + w2 * c2[0] + w3 * p1[0], w0 * p0[1] + w1 * c1[1] + w2 * c2[1] + w3 * p1[1])
    }
  }
  if (!closed) out.push(P[m - 1].co[0], P[m - 1].co[1])
  return out
}

export function ringArea(r: ArrayLike<number>): number {
  const n = r.length >> 1
  let a = 0
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    a += r[2 * i] * r[2 * j + 1] - r[2 * j] * r[2 * i + 1]
  }
  return a / 2
}

type P2 = [number, number]

function isSpike(a: P2, v: P2, c: P2, wEps: number): boolean {
  const ux = v[0] - a[0]
  const uy = v[1] - a[1]
  const wx = c[0] - v[0]
  const wy = c[1] - v[1]
  const lu = Math.hypot(ux, uy)
  const lw = Math.hypot(wx, wy)
  if (lu < 1e-300 || lw < 1e-300) return true
  if ((ux * wx + uy * wy) / (lu * lw) > -0.9) return false
  const cr = Math.abs(ux * wy - uy * wx)
  return cr / lu < wEps || cr / lw < wEps
}

/**
 * Merge near-duplicate consecutive points (incl. the wrap) and remove zero-width spikes and slits narrower than
 * wEps = 20·eps (boolean-union seams), peeled from the tip inward.
 */
export function cleanRing(src: number[], eps: number, wEps = 20 * eps): number[] {
  const n0 = src.length >> 1
  if (n0 < 3) return src
  let r: number[] = []
  for (let i = 0; i < n0; i++) {
    const j = (i - 1 + n0) % n0
    if (Math.hypot(src[2 * i] - src[2 * j], src[2 * i + 1] - src[2 * j + 1]) > eps) r.push(src[2 * i], src[2 * i + 1])
  }
  if (!r.length) return src.slice(0, 2)
  const n = r.length >> 1
  if (n < 3) return r
  let fold = false
  for (let i = 0; i < n && !fold; i++) {
    const p = (i - 1 + n) % n
    const q = (i + 1) % n
    const ax = r[2 * i] - r[2 * p]
    const ay = r[2 * i + 1] - r[2 * p + 1]
    const bx = r[2 * q] - r[2 * i]
    const by = r[2 * q + 1] - r[2 * i + 1]
    const c = (ax * bx + ay * by) / Math.max(Math.hypot(ax, ay) * Math.hypot(bx, by), 1e-300)
    if (c < -0.9) fold = true
  }
  if (!fold) return r
  let pts: P2[] = []
  for (let i = 0; i < n; i++) pts.push([r[2 * i], r[2 * i + 1]])
  const close = (a: P2, b: P2) => Math.hypot(a[0] - b[0], a[1] - b[1]) <= eps
  for (let pass = 0; pass < 2; pass++) {
    const out: P2[] = []
    for (const p of pts) {
      if (out.length && close(p, out[out.length - 1])) continue
      out.push(p)
      while (out.length >= 3 && isSpike(out[out.length - 3], out[out.length - 2], out[out.length - 1], wEps)) {
        out.splice(out.length - 2, 1)
        if (out.length >= 2 && close(out[out.length - 1], out[out.length - 2])) out.pop()
      }
    }
    if (out.length < 3) {
      r = []
      for (const p of out) r.push(p[0], p[1])
      return r
    }
    const h = out.length >> 1
    pts = [...out.slice(h), ...out.slice(0, h)]
  }
  while (pts.length >= 3 && isSpike(pts[pts.length - 2], pts[pts.length - 1], pts[0], wEps)) pts.pop()
  r = []
  for (const p of pts) r.push(p[0], p[1])
  return r
}

/** Guard points at `dist` from every corner sharper than minTurnDeg on both adjacent edges (edges > 3·dist). */
export function guardRing(r: number[], dist: number, minTurnDeg = CORNER_DEG): number[] {
  const n = r.length >> 1
  if (n < 3 || dist <= 0) return r
  const cosLim = Math.cos((minTurnDeg * Math.PI) / 180)
  const sharp = new Uint8Array(n)
  const lb = new Float64Array(n)
  let any = false
  for (let i = 0; i < n; i++) {
    const p = (i - 1 + n) % n
    const q = (i + 1) % n
    const ax = r[2 * i] - r[2 * p]
    const ay = r[2 * i + 1] - r[2 * p + 1]
    const bx = r[2 * q] - r[2 * i]
    const by = r[2 * q + 1] - r[2 * i + 1]
    lb[i] = Math.hypot(bx, by)
    const c = (ax * bx + ay * by) / Math.max(Math.hypot(ax, ay) * lb[i], 1e-300)
    if (c < cosLim) {
      sharp[i] = 1
      any = true
    }
  }
  if (!any) return r
  const out: number[] = []
  for (let i = 0; i < n; i++) {
    const q = (i + 1) % n
    const px = r[2 * i]
    const py = r[2 * i + 1]
    out.push(px, py)
    const L = lb[i]
    if (L <= 3 * dist) continue
    const ux = (r[2 * q] - px) / L
    const uy = (r[2 * q + 1] - py) / L
    if (sharp[i]) out.push(px + ux * dist, py + uy * dist)
    if (sharp[q]) out.push(r[2 * q] - ux * dist, r[2 * q + 1] - uy * dist)
  }
  return out
}

/** The SHAPE ring of a sample ring: the same polyline without its (nearly) collinear points. */
export function simplifyRing(r: number[], tol: number): number[] {
  let out = r
  for (let it = 0; it < 16; it++) {
    const n = out.length >> 1
    if (n <= 4) return out
    const rem = new Uint8Array(n)
    let anyRem = false
    for (let i = 0; i < n; i++) {
      const a = (i - 1 + n) % n
      const c = (i + 1) % n
      const acx = out[2 * c] - out[2 * a]
      const acy = out[2 * c + 1] - out[2 * a + 1]
      const lac = Math.max(Math.hypot(acx, acy), 1e-300)
      const ox = out[2 * i] - out[2 * a]
      const oy = out[2 * i + 1] - out[2 * a + 1]
      const dev = Math.abs(ox * acy - oy * acx) / lac
      const t = (ox * acx + oy * acy) / (lac * lac)
      if (dev < tol && t > 0 && t < 1) {
        rem[i] = 1
        anyRem = true
      }
    }
    if (!anyRem) return out
    let left = false
    for (let i = 0; i < n; i++) {
      if (rem[i] && i % 2 !== it % 2) rem[i] = 0
      if (n % 2 === 1 && i === n - 1) rem[i] = 0
      if (rem[i]) left = true
    }
    if (!left) continue
    const next: number[] = []
    for (let i = 0; i < n; i++) if (!rem[i]) next.push(out[2 * i], out[2 * i + 1])
    out = next
  }
  return out
}

function pointInRing(x: number, y: number, r: ArrayLike<number>): boolean {
  const n = r.length >> 1
  let inside = false
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    const x0 = r[2 * i]
    const y0 = r[2 * i + 1]
    const x1 = r[2 * j]
    const y1 = r[2 * j + 1]
    if (y0 > y !== y1 > y) {
      const dy = Math.abs(y1 - y0) < 1e-300 ? 1e-300 : y1 - y0
      if (x < x0 + ((y - y0) * (x1 - x0)) / dy) inside = !inside
    }
  }
  return inside
}

function reversed(r: number[]): number[] {
  const n = r.length >> 1
  const out = new Array<number>(r.length)
  for (let i = 0; i < n; i++) {
    out[2 * i] = r[2 * (n - 1 - i)]
    out[2 * i + 1] = r[2 * (n - 1 - i) + 1]
  }
  return out
}

function ringBox(r: ArrayLike<number>): [number, number, number, number] {
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  for (let i = 0; i < r.length; i += 2) {
    if (r[i] < x0) x0 = r[i]
    if (r[i] > x1) x1 = r[i]
    if (r[i + 1] < y0) y0 = r[i + 1]
    if (r[i + 1] > y1) y1 = r[i + 1]
  }
  return [x0, y0, x1, y1]
}

/** Even-odd nesting -> rings oriented outer CCW / hole CW, hole flags, island id (outer ring index) per ring. */
export function organise(rings: number[][]): { rings: number[][]; hole: boolean[]; island: number[] } {
  const R = rings.length
  const bb = rings.map(ringBox)
  const areas = rings.map(ringArea)
  const depth = new Array<number>(R).fill(0)
  const contains: boolean[][] = rings.map(() => new Array<boolean>(R).fill(false)) // contains[j][i]
  for (let i = 0; i < R; i++) {
    const ri = rings[i]
    const n = ri.length >> 1
    const probes = [0, Math.floor(n / 3), Math.floor((2 * n) / 3)]
    for (let j = 0; j < R; j++) {
      if (j === i || Math.abs(areas[j]) < Math.abs(areas[i])) continue
      if (bb[j][0] > bb[i][0] || bb[j][1] > bb[i][1] || bb[j][2] < bb[i][2] || bb[j][3] < bb[i][3]) continue
      let k = 0
      for (const p of probes) if (pointInRing(ri[2 * p], ri[2 * p + 1], rings[j])) k++
      if (k >= 2) {
        contains[j][i] = true
        depth[i]++
      }
    }
  }
  const hole = depth.map((d) => d % 2 === 1)
  const island = rings.map((_, i) => i)
  for (let i = 0; i < R; i++) {
    if (!hole[i]) continue
    let best = -1
    for (let j = 0; j < R; j++)
      if (contains[j][i] && depth[j] === depth[i] - 1 && (best < 0 || Math.abs(areas[j]) < Math.abs(areas[best]))) best = j
    if (best >= 0) island[i] = best
  }
  const out = rings.map((r, i) => ((areas[i] > 0) !== hole[i] ? r : reversed(r)))
  return { rings: out, hole, island }
}

export interface Outlines {
  /** Sample rings (mesh outline), oriented outer CCW / hole CW. */
  rings: Ring[]
  /** SHAPE rings (same outline, no collinear subdivisions) for deep distance queries. */
  shapes: Ring[]
  hole: boolean[]
  island: number[]
}

/** Splines -> oriented sample rings + shape rings, hole flags, island per ring (degenerate rings dropped). */
export function outline(splines: Spline[], tol: number, maxEdge: number, eps: number, guard = 0): Outlines {
  const rings: number[][] = []
  const shapes: number[][] = []
  for (const s of splines ?? []) {
    if ((s.points ?? []).length < 2) continue
    let r = cleanRing(flattenSpline(s, tol, maxEdge), eps)
    if (r.length < 6 || Math.abs(ringArea(r)) < MIN_AREA) continue
    const c = simplifyRing(r, 0.05 * tol)
    if (guard > 0) r = guardRing(r, guard)
    rings.push(r)
    shapes.push(c)
  }
  if (!rings.length) return { rings: [], shapes: [], hole: [], island: [] }
  const o = organise(rings)
  const sh = shapes.map((c, i) => ((ringArea(c) > 0) === (ringArea(o.rings[i]) > 0) ? c : reversed(c)))
  return {
    rings: o.rings.map((r) => Float64Array.from(r)),
    shapes: sh.map((r) => Float64Array.from(r)),
    hole: o.hole,
    island: o.island,
  }
}

// ================================================================================================ distances
/** Segment soup of rings with a uniform grid for exact nearest / softmin queries. */
class SegmentSet {
  readonly ax: Float64Array
  readonly ay: Float64Array
  readonly bx: Float64Array
  readonly by: Float64Array
  readonly ringOf: Int32Array
  readonly prev: Int32Array
  readonly next: Int32Array
  private readonly x0: number
  private readonly y0: number
  private readonly cell: number
  private readonly nx: number
  private readonly ny: number
  private readonly start: Int32Array
  private readonly items: Int32Array
  private readonly stamp: Int32Array
  private tick = 0

  constructor(rings: Ring[]) {
    let M = 0
    for (const r of rings) M += r.length >> 1
    this.ax = new Float64Array(M)
    this.ay = new Float64Array(M)
    this.bx = new Float64Array(M)
    this.by = new Float64Array(M)
    this.ringOf = new Int32Array(M)
    this.prev = new Int32Array(M)
    this.next = new Int32Array(M)
    this.stamp = new Int32Array(M)
    let s = 0
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    let total = 0
    rings.forEach((r, k) => {
      const n = r.length >> 1
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n
        const g = s + i
        this.ax[g] = r[2 * i]
        this.ay[g] = r[2 * i + 1]
        this.bx[g] = r[2 * j]
        this.by[g] = r[2 * j + 1]
        this.ringOf[g] = k
        this.prev[g] = s + ((i - 1 + n) % n)
        this.next[g] = s + j
        total += Math.hypot(r[2 * j] - r[2 * i], r[2 * j + 1] - r[2 * i + 1])
        if (r[2 * i] < minX) minX = r[2 * i]
        if (r[2 * i] > maxX) maxX = r[2 * i]
        if (r[2 * i + 1] < minY) minY = r[2 * i + 1]
        if (r[2 * i + 1] > maxY) maxY = r[2 * i + 1]
      }
      s += n
    })
    const ext = Math.max(maxX - minX, maxY - minY, 1e-9)
    // ~4 segments per occupied cell on average, at most 256 cells per side
    const cell = Math.max(ext / 256, Math.min(ext / 8, M ? (4 * total) / M : ext))
    this.cell = cell
    this.x0 = minX - cell * 0.5
    this.y0 = minY - cell * 0.5
    this.nx = Math.max(1, Math.ceil((maxX - this.x0) / cell) + 1)
    this.ny = Math.max(1, Math.ceil((maxY - this.y0) / cell) + 1)
    const counts = new Int32Array(this.nx * this.ny + 1)
    const span = (g: number): [number, number, number, number] => [
      Math.max(0, Math.min(this.nx - 1, Math.floor((Math.min(this.ax[g], this.bx[g]) - this.x0) / cell))),
      Math.max(0, Math.min(this.ny - 1, Math.floor((Math.min(this.ay[g], this.by[g]) - this.y0) / cell))),
      Math.max(0, Math.min(this.nx - 1, Math.floor((Math.max(this.ax[g], this.bx[g]) - this.x0) / cell))),
      Math.max(0, Math.min(this.ny - 1, Math.floor((Math.max(this.ay[g], this.by[g]) - this.y0) / cell))),
    ]
    for (let g = 0; g < M; g++) {
      const [i0, j0, i1, j1] = span(g)
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) counts[j * this.nx + i + 1]++
    }
    for (let c = 1; c < counts.length; c++) counts[c] += counts[c - 1]
    this.start = counts
    this.items = new Int32Array(counts[counts.length - 1])
    const fill = counts.slice(0, -1)
    for (let g = 0; g < M; g++) {
      const [i0, j0, i1, j1] = span(g)
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) this.items[fill[j * this.nx + i]++] = g
    }
  }

  get size(): number {
    return this.ax.length
  }

  /** Distance from (px, py) to segment g; writes the foot-to-point vector into out. */
  segDist(g: number, px: number, py: number, out?: Float64Array): number {
    const ax = this.ax[g]
    const ay = this.ay[g]
    const abx = this.bx[g] - ax
    const aby = this.by[g] - ay
    const L2 = abx * abx + aby * aby
    const apx = px - ax
    const apy = py - ay
    let t = L2 > 1e-30 ? (apx * abx + apy * aby) / L2 : 0
    t = t < 0 ? 0 : t > 1 ? 1 : t
    const dx = apx - t * abx
    const dy = apy - t * aby
    if (out) {
      out[0] = dx
      out[1] = dy
    }
    return Math.sqrt(dx * dx + dy * dy)
  }

  /**
   * Visit every segment whose distance to p may be ≤ limit(best) — cells are scanned in growing square rings until the
   * ring's lower distance bound exceeds the (shrinking or fixed) limit. `visit` returns the current limit.
   */
  scan(px: number, py: number, visit: (g: number) => number): void {
    if (!this.size) return
    const cell = this.cell
    const fi = (px - this.x0) / cell
    const fj = (py - this.y0) / cell
    const ci = Math.max(0, Math.min(this.nx - 1, Math.floor(fi)))
    const cj = Math.max(0, Math.min(this.ny - 1, Math.floor(fj)))
    // distance from p to its (clamped) start cell box: rings beyond r are at least (r − 1)·cell + base away
    const bx = Math.max(this.x0 + ci * cell - px, 0, px - (this.x0 + (ci + 1) * cell))
    const by = Math.max(this.y0 + cj * cell - py, 0, py - (this.y0 + (cj + 1) * cell))
    const base = Math.hypot(bx, by)
    this.tick++
    if (this.tick > 2e9) {
      this.stamp.fill(0)
      this.tick = 1
    }
    const tick = this.tick
    let limit = Infinity
    const maxR = Math.max(this.nx, this.ny)
    for (let r = 0; r <= maxR; r++) {
      if (r > 0 && Math.max(base, (r - 1) * cell) > limit) break
      const i0 = ci - r
      const i1 = ci + r
      const j0 = cj - r
      const j1 = cj + r
      for (let j = j0; j <= j1; j++) {
        if (j < 0 || j >= this.ny) continue
        const edge = j === j0 || j === j1
        for (let i = i0; i <= i1; i += edge ? 1 : i1 - i0 || 1) {
          if (i < 0 || i >= this.nx) continue
          const c = j * this.nx + i
          for (let s = this.start[c]; s < this.start[c + 1]; s++) {
            const g = this.items[s]
            if (this.stamp[g] === tick) continue
            this.stamp[g] = tick
            limit = visit(g)
          }
        }
      }
    }
  }
}

const _v = new Float64Array(2)

export interface NearestResult {
  d: Float64Array
  seg: Int32Array
  /** Unit direction (or the softmin blend) from the outline toward each point. */
  g: Float64Array
}

/**
 * Distance of points P (flat x,y) to the segments -> (d, segment, g). With kappa > 0, g is the two-cluster softmin
 * over FOOT POINTS (segments whose distance is a local minimum along their ring), weights exp(−(dist − d)/κ),
 * κ = max(kappa, rel·d) — heightfield.nearest with `nbr`.
 */
function nearest(P: ArrayLike<number>, S: SegmentSet, kappa = 0, rel = 0): NearestResult {
  const N = P.length >> 1
  const d = new Float64Array(N)
  const seg = new Int32Array(N)
  const g = new Float64Array(N * 2)
  if (!N || !S.size) return { d, seg, g }
  const gathered: number[] = []
  const gdist: number[] = []
  const gvx: number[] = []
  const gvy: number[] = []
  const local = new Map<number, number>()
  for (let q = 0; q < N; q++) {
    const px = P[2 * q]
    const py = P[2 * q + 1]
    let best = Infinity
    let bestG = 0
    let bvx = 0
    let bvy = 0
    S.scan(px, py, (k) => {
      const dist = S.segDist(k, px, py, _v)
      if (dist < best) {
        best = dist
        bestG = k
        bvx = _v[0]
        bvy = _v[1]
      }
      return best
    })
    d[q] = best
    seg[q] = bestG
    const dm = Math.max(best, 1e-30)
    if (!(kappa > 0)) {
      g[2 * q] = bvx / dm
      g[2 * q + 1] = bvy / dm
      continue
    }
    const kap = Math.max(kappa, rel * best)
    const reach = best + 2 * SOFT_REACH * kap
    gathered.length = 0
    gdist.length = 0
    gvx.length = 0
    gvy.length = 0
    local.clear()
    S.scan(px, py, (k) => {
      const dist = S.segDist(k, px, py, _v)
      if (dist <= reach) {
        local.set(k, gathered.length)
        gathered.push(k)
        gdist.push(dist)
        gvx.push(_v[0])
        gvy.push(_v[1])
      }
      return reach
    })
    const ux0 = bvx / dm
    const uy0 = bvy / dm
    let Ws = 0
    let Wo = 0
    let sx = 0
    let sy = 0
    let ox = 0
    let oy = 0
    const tie = 1e-6
    for (let m = 0; m < gathered.length; m++) {
      const dist = gdist[m]
      if (dist <= 1e-30) continue
      const k = gathered[m]
      const ip = local.get(S.prev[k])
      const inx = local.get(S.next[k])
      const dp = ip === undefined ? Infinity : gdist[ip]
      const dn = inx === undefined ? Infinity : gdist[inx]
      if (!(dist <= dp + tie && dist <= dn + tie)) continue
      const w = Math.exp(-(dist - best) / kap)
      const ux = gvx[m] / dist
      const uy = gvy[m] / dist
      if (ux * ux0 + uy * uy0 > 0.5) {
        Ws += w
        sx += w * ux
        sy += w * uy
      } else {
        Wo += w
        ox += w * ux
        oy += w * uy
      }
    }
    const sn = Math.max(Math.hypot(sx, sy), 1e-30)
    const on = Math.max(Math.hypot(ox, oy), 1e-30)
    const tot = Math.max(Ws + Wo, 1e-30)
    g[2 * q] = (Ws * (sx / sn) + Wo * (ox / on)) / tot
    g[2 * q + 1] = (Ws * (sy / sn) + Wo * (oy / on)) / tot
  }
  return { d, seg, g }
}

/** Even-odd raster (ys.length × xs.length) of closed rings: one scanline per row. */
function scanInside(rings: Ring[], xs: number[], ys: number[]): Uint8Array {
  const out = new Uint8Array(xs.length * ys.length)
  const xc: number[] = []
  ys.forEach((y, j) => {
    xc.length = 0
    for (const r of rings) {
      const n = r.length >> 1
      for (let i = 0; i < n; i++) {
        const k = (i + 1) % n
        const ya = r[2 * i + 1]
        const yb = r[2 * k + 1]
        if (ya > y !== yb > y) xc.push(r[2 * i] + ((y - ya) * (r[2 * k] - r[2 * i])) / (yb - ya))
      }
    }
    if (!xc.length) return
    xc.sort((a, b) => a - b)
    let c = 0
    xs.forEach((x, i) => {
      while (c < xc.length && xc[c] < x) c++
      if (c % 2 === 1) out[j * xs.length + i] = 1
    })
  })
  return out
}

/** Distance queries against a piece outline: deep points on the SHAPE rings, points closer than `near` re-measured
 * on the SAMPLE rings (the mesh outline itself). */
export class OutlineQuery {
  readonly o: Outlines
  readonly near: number
  readonly sample: SegmentSet
  readonly shape: SegmentSet
  constructor(o: Outlines, near: number) {
    this.o = o
    this.near = near
    this.sample = new SegmentSet(o.rings)
    this.shape = new SegmentSet(o.shapes)
  }

  /** -> (d, island id, g) of points P. */
  query(P: ArrayLike<number>, kappa = 0, rel = 0): { d: Float64Array; isl: Int32Array; g: Float64Array } {
    const r = nearest(P, this.shape, kappa, rel)
    const N = r.d.length
    const isl = new Int32Array(N)
    const close: number[] = []
    const idx: number[] = []
    for (let q = 0; q < N; q++) {
      isl[q] = this.o.island[this.shape.ringOf[r.seg[q]]]
      if (r.d[q] < this.near) {
        idx.push(q)
        close.push(P[2 * q], P[2 * q + 1])
      }
    }
    if (idx.length) {
      const r2 = nearest(close, this.sample, kappa, rel)
      idx.forEach((q, m) => {
        r.d[q] = r2.d[m]
        r.g[2 * q] = r2.g[2 * m]
        r.g[2 * q + 1] = r2.g[2 * m + 1]
        isl[q] = this.o.island[this.sample.ringOf[r2.seg[m]]]
      })
    }
    return { d: r.d, isl, g: r.g }
  }
}

const DIRS8: P2[] = [
  [1, 0],
  [-1, 0],
  [0, 1],
  [0, -1],
  [0.7, 0.7],
  [-0.7, 0.7],
  [0.7, -0.7],
  [-0.7, -0.7],
]

/** numpy.arange(start, stop, step): ceil((stop − start) / step) values start + i·step. */
function arange(start: number, stop: number, step: number): number[] {
  const n = Math.max(0, Math.ceil((stop - start) / step))
  return Array.from({ length: n }, (_, i) => start + i * step)
}

/**
 * Estimated inradius D per ring's island (holes carry their island's value): max d over a grid × grid scanline raster
 * refined by 12 steps of pattern search from each island's best cell; `centres` receives {island: apex}.
 * DETERMINISTIC APEX (round 8, heightfield.island_inradius — float32 there, float64 here, so exact ties must not decide):
 * the island's tied cells are those with d ≥ max d − APEX_TIE·h; the start cell is the tied cell nearest the tied cells'
 * centroid (the first in list order — rows bottom → top, x left → right — within APEX_TIE·h of the nearest); every move
 * takes the FIRST of the eight directions within APEX_TIE·h of their best, and only when it beats the current d by
 * more than APEX_TIE·h.
 */
export function islandInradius(ol: OutlineQuery, grid = 40, centres?: Map<number, P2>): Float64Array {
  const rings = ol.o.shapes
  const island = ol.o.island
  const R = rings.length
  const D = new Float64Array(R)
  if (!R) return D
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  for (const r of rings) {
    const b = ringBox(r)
    x0 = Math.min(x0, b[0])
    y0 = Math.min(y0, b[1])
    x1 = Math.max(x1, b[2])
    y1 = Math.max(y1, b[3])
  }
  const h = Math.max(x1 - x0, y1 - y0) / grid
  if (!(h > 0)) return D
  const xs = arange(x0 + h / 2, x1, h)
  const ys = arange(y0 + h / 2, y1, h)
  const ins = scanInside(rings, xs, ys)
  const P: number[] = []
  ys.forEach((y, j) => xs.forEach((x, i) => ins[j * xs.length + i] && P.push(x, y)))
  if (P.length) {
    const near = nearest(P, ol.shape)
    const N = P.length >> 1
    const tie = APEX_TIE * h
    const members = new Map<number, number[]>() // island -> its cells, in list order
    for (let q = 0; q < N; q++) {
      const k = island[ol.shape.ringOf[near.seg[q]]]
      if (near.d[q] > D[k]) D[k] = near.d[q]
      let m = members.get(k)
      if (!m) members.set(k, (m = []))
      m.push(q)
    }
    for (const k of [...members.keys()].sort((a, b) => a - b)) {
      const sel = members.get(k)!
      let dmax = -Infinity
      for (const q of sel) dmax = Math.max(dmax, near.d[q])
      const tied = sel.filter((q) => near.d[q] >= dmax - tie)
      let cx = 0
      let cy = 0
      for (const q of tied) {
        cx += P[2 * q]
        cy += P[2 * q + 1]
      }
      cx /= tied.length
      cy /= tied.length
      const dist = tied.map((q) => Math.hypot(P[2 * q] - cx, P[2 * q + 1] - cy))
      const dmin = Math.min(...dist)
      const start = tied[dist.findIndex((v) => v <= dmin + tie)]
      let px = P[2 * start]
      let py = P[2 * start + 1]
      let step = 0.5 * h
      let best = near.d[start]
      for (let it = 0; it < 12; it++) {
        const cand: number[] = []
        for (const [dx, dy] of DIRS8) cand.push(px + step * dx, py + step * dy)
        const r = nearest(cand, ol.shape)
        const val: number[] = []
        for (let c = 0; c < 8; c++) val.push(island[ol.shape.ringOf[r.seg[c]]] === k ? r.d[c] : -1)
        const vmax = Math.max(...val)
        const j = val.findIndex((v) => v >= vmax - tie) // the first direction tied with the best
        if (val[j] > best + tie) {
          best = val[j]
          px = cand[2 * j]
          py = cand[2 * j + 1]
        } else step *= 0.5
      }
      D[k] = Math.max(D[k], best)
      centres?.set(k, [px, py])
    }
  }
  // tiny islands the grid missed: a lower bound from the ring's own size
  for (let k = 0; k < R; k++) {
    if (island[k] === k && D[k] <= 0) {
      const b = ringBox(rings[k])
      D[k] = 0.25 * Math.min(b[2] - b[0], b[3] - b[1])
    }
  }
  return Float64Array.from(island, (k) => D[k])
}

// ================================================================================================ local half-width
const f32 = Math.fround

/**
 * The segment the WORKER takes as nearest to p, among segment k (this module's float64 choice) and its ring neighbours
 * sharing its endpoints: heightfield.nearest runs in float32 (its hot loop) and takes numpy's argmin, the lowest index
 * among equal distances. Where p's nearest outline point is a vertex shared by two segments, both are (almost exactly)
 * as near, and at a sharp corner the material-side test of localWidth depends on which one is taken — mirroring the
 * worker's float32 arithmetic keeps the local half-width (hence the capped round edge) identical to the worker's
 * (without it ~0.4 % of the corpus' outline vertices got a different width, up to 17 % of the bevel at Earth's coast).
 */
function workerNearestSeg(S: SegmentSet, k: number, px: number, py: number): number {
  const x = f32(px)
  const y = f32(py)
  let best = k
  let bd = Infinity
  for (const c of [k, S.prev[k], S.next[k]]) {
    // segprep: A and B − A as float32, 1/|B − A|² from float64; nearest: (P − A)·(B − A)·inv clipped, then the residual
    const ex = S.bx[c] - S.ax[c]
    const ey = S.by[c] - S.ay[c]
    const L2 = ex * ex + ey * ey
    const inv = f32(L2 > 1e-30 ? 1 / L2 : 0)
    const bx = f32(ex)
    const by = f32(ey)
    const apx = f32(x - f32(S.ax[c]))
    const apy = f32(y - f32(S.ay[c]))
    let t = f32(f32(f32(apx * bx) + f32(apy * by)) * inv)
    t = t < 0 ? 0 : t > 1 ? 1 : t
    const dx = f32(apx - f32(t * bx))
    const dy = f32(apy - f32(t * by))
    const d = f32(Math.sqrt(f32(f32(dx * dx) + f32(dy * dy))))
    if (d < bd || (d === bd && c < best)) {
      bd = d
      best = c
    }
  }
  return best
}

/**
 * LOCAL HALF-WIDTH w at every sample-ring vertex (local units, in the sample segment order; heightfield.local_width,
 * SAMPLING 3c): the radius of the largest disc tangent to the outline at the vertex — centred on its inward bisector —
 * that stays inside the piece (centre on the material side of its nearest shape segment, true distance ≥
 * (1 − RING_TOL)·r − tolAbs), by WIDTH_BISECT bisection steps in [0, 1.05·D] (`Dr`: the inradius per ring). `window`
 * (the bevel b): then the sliding MAX over ±b of arclength along the ring, then the sliding MEAN over ±b/2 — a part is
 * thin where it stays thin along its outline, not at the corner of a wide part.
 */
export function localWidth(ol: OutlineQuery, Dr: ArrayLike<number>, tolAbs: number, window = 0): Float64Array {
  const rings = ol.o.rings
  let M = 0
  for (const r of rings) M += r.length >> 1
  const O = new Float64Array(2 * M)
  const Nd = new Float64Array(2 * M)
  const lo = new Float64Array(M)
  const hi = new Float64Array(M)
  let s = 0
  rings.forEach((r, k) => {
    const n = r.length >> 1
    const Dk = k < Dr.length ? Dr[k] : 0
    for (let i = 0; i < n; i++) {
      const p = (i - 1 + n) % n
      const q = (i + 1) % n
      let tix = r[2 * i] - r[2 * p]
      let tiy = r[2 * i + 1] - r[2 * p + 1]
      let l = Math.max(Math.hypot(tix, tiy), 1e-300)
      tix /= l
      tiy /= l
      let tox = r[2 * q] - r[2 * i]
      let toy = r[2 * q + 1] - r[2 * i + 1]
      l = Math.max(Math.hypot(tox, toy), 1e-300)
      tox /= l
      toy /= l
      const bx = -tiy - toy
      const by = tix + tox
      const bn = Math.hypot(bx, by)
      const g = s + i
      O[2 * g] = r[2 * i]
      O[2 * g + 1] = r[2 * i + 1]
      if (bn > 1e-6) {
        Nd[2 * g] = bx / bn
        Nd[2 * g + 1] = by / bn
      } else {
        Nd[2 * g] = -tiy
        Nd[2 * g + 1] = tix
      }
      hi[g] = 1.05 * Dk + tolAbs
    }
    s += n
  })
  if (!M) return lo
  const Q = new Float64Array(2 * M)
  const mid = new Float64Array(M)
  const S = ol.shape
  for (let it = 0; it < WIDTH_BISECT; it++) {
    for (let g = 0; g < M; g++) {
      mid[g] = 0.5 * (lo[g] + hi[g])
      Q[2 * g] = O[2 * g] + Nd[2 * g] * mid[g]
      Q[2 * g + 1] = O[2 * g + 1] + Nd[2 * g + 1] * mid[g]
    }
    const r = nearest(Q, S)
    for (let g = 0; g < M; g++) {
      // the worker's segment where the nearest point is a vertex shared by two segments (workerNearestSeg)
      const sg = workerNearestSeg(S, r.seg[g], Q[2 * g], Q[2 * g + 1])
      const dg = S.segDist(sg, Q[2 * g], Q[2 * g + 1], _v)
      const ex = S.bx[sg] - S.ax[sg]
      const ey = S.by[sg] - S.ay[sg]
      const inside = _v[1] * ex - _v[0] * ey > 0 // on the material side (left) of its segment
      if (inside && dg >= (1 - RING_TOL) * mid[g] - tolAbs) lo[g] = mid[g]
      else hi[g] = mid[g]
    }
  }
  if (window > 0) return slide(slide(lo, rings, window, 'max'), rings, 0.5 * window, 'mean')
  return lo
}

/** First index of the sorted `a` with a[i] ≥ x (side 'left') / a[i] > x (side 'right') — numpy.searchsorted. */
function searchSorted(a: ArrayLike<number>, x: number, right: boolean): number {
  let lo = 0
  let hi = a.length
  while (lo < hi) {
    const m = (lo + hi) >> 1
    if (right ? a[m] <= x : a[m] < x) lo = m + 1
    else hi = m
  }
  return lo
}

/** Sliding max / mean of a per-vertex value along each closed ring over the arclength window ±half (heightfield._slide). */
function slide(v: Float64Array, rings: Ring[], half: number, op: 'max' | 'mean'): Float64Array {
  const out = Float64Array.from(v)
  let s0 = 0
  for (const r of rings) {
    const n = r.length >> 1
    const seg = new Float64Array(n)
    let Lr = 0
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n
      seg[i] = Math.hypot(r[2 * j] - r[2 * i], r[2 * j + 1] - r[2 * i + 1])
      Lr += seg[i]
    }
    if (n < 3 || !(Lr > 0)) {
      s0 += n
      continue
    }
    const sv = new Float64Array(n)
    for (let i = 1; i < n; i++) sv[i] = sv[i - 1] + seg[i - 1]
    const s3 = new Float64Array(3 * n)
    const v3 = new Float64Array(3 * n)
    for (let i = 0; i < n; i++) {
      s3[i] = sv[i] - Lr
      s3[n + i] = sv[i]
      s3[2 * n + i] = sv[i] + Lr
      v3[i] = v3[n + i] = v3[2 * n + i] = v[s0 + i]
    }
    const c = new Float64Array(3 * n + 1)
    for (let i = 0; i < 3 * n; i++) c[i + 1] = c[i] + v3[i]
    const h = Math.min(half, 0.5 * Lr)
    for (let i = 0; i < n; i++) {
      const a = searchSorted(s3, sv[i] - h, false)
      const b = searchSorted(s3, sv[i] + h, true)
      if (op === 'max') {
        let m = -Infinity
        for (let k = a; k < b; k++) if (v3[k] > m) m = v3[k]
        out[s0 + i] = m
      } else out[s0 + i] = (c[b] - c[a]) / Math.max(b - a, 1)
    }
    s0 += n
  }
  return out
}

export interface WidthResult {
  /** Local half-width at each point, its gradient (x, y pairs), the distance to the sample outline, its gradient. */
  w: Float64Array
  gw: Float64Array
  d: Float64Array
  gd: Float64Array
}

/**
 * (w, ∇w, d, ∇d) at points P (heightfield.blend_width): d = the distance to the sample outline; w = the local
 * half-width `wv` at P's FOOT POINTS — sample segments whose distance is a local minimum along their ring and at most
 * d·(1 + 5·WIDTH_BLEND) away (the nearest one always) — each interpolated linearly at P's projection onto it, the feet
 * weighted by exp(−(dist − d) / (WIDTH_BLEND·d)). On a medial axis both sides blend evenly (no crease). Analytic
 * gradients (feet fixed).
 */
export function blendWidth(ol: OutlineQuery, wv: ArrayLike<number>, P: ArrayLike<number>): WidthResult {
  const n = P.length >> 1
  const out: WidthResult = { w: new Float64Array(n), gw: new Float64Array(2 * n), d: new Float64Array(n), gd: new Float64Array(2 * n) }
  const S = ol.sample
  if (!n || !S.size) return out
  const fk: number[] = []
  const fdist: number[] = []
  const fqx: number[] = []
  const fqy: number[] = []
  const ft: number[] = []
  const fraw: number[] = []
  const tie = 1e-6
  for (let q = 0; q < n; q++) {
    const px = P[2 * q]
    const py = P[2 * q + 1]
    let best = Infinity
    S.scan(px, py, (k) => {
      const dist = S.segDist(k, px, py)
      if (dist < best) best = dist
      return best
    })
    const kap = WIDTH_BLEND * Math.max(best, 1e-12)
    const reach = best + 5 * kap
    fk.length = fdist.length = fqx.length = fqy.length = ft.length = fraw.length = 0
    let jmin = -1
    S.scan(px, py, (k) => {
      const ax = S.ax[k]
      const ay = S.ay[k]
      const ex = S.bx[k] - ax
      const ey = S.by[k] - ay
      const L2 = Math.max(ex * ex + ey * ey, 1e-300)
      const traw = ((px - ax) * ex + (py - ay) * ey) / L2
      const t = traw < 0 ? 0 : traw > 1 ? 1 : traw
      const qx = px - ax - t * ex
      const qy = py - ay - t * ey
      const dist = Math.sqrt(qx * qx + qy * qy)
      if (dist <= reach + 1e-12) {
        const isMin = dist <= best
        if (!isMin && !(dist <= S.segDist(S.prev[k], px, py) + tie && dist <= S.segDist(S.next[k], px, py) + tie)) return reach
        if (isMin && jmin < 0) jmin = fk.length
        fk.push(k)
        fdist.push(dist)
        fqx.push(qx)
        fqy.push(qy)
        ft.push(t)
        fraw.push(traw)
      }
      return reach
    })
    if (jmin < 0) continue
    const dm = Math.max(best, 1e-300)
    const usx = fqx[jmin] / dm
    const usy = fqy[jmin] / dm
    let Ws = 0
    let Sw = 0
    const W: number[] = []
    const wi: number[] = []
    for (let m = 0; m < fk.length; m++) {
      const k = fk[m]
      const dd = Math.max(fdist[m], 1e-300)
      const Wf = Math.exp(-(dd - best) / kap)
      const w0 = wv[k]
      const w1 = wv[S.next[k]]
      const v = w0 + ft[m] * (w1 - w0)
      W.push(Wf)
      wi.push(v)
      Ws += Wf
      Sw += Wf * v
    }
    Ws = Math.max(Ws, 1e-300)
    const wb = Sw / Ws
    let gx = 0
    let gy = 0
    const k2 = kap * kap
    for (let m = 0; m < fk.length; m++) {
      const k = fk[m]
      const dd = Math.max(fdist[m], 1e-300)
      const ux = fqx[m] / dd
      const uy = fqy[m] / dd
      const ex = S.bx[k] - S.ax[k]
      const ey = S.by[k] - S.ay[k]
      const L2 = Math.max(ex * ex + ey * ey, 1e-300)
      const inseg = fraw[m] > 0 && fraw[m] < 1 ? (wv[S.next[k]] - wv[k]) / L2 : 0
      const dl = dd - best
      const glx = -((ux - usx) / kap - (dl * WIDTH_BLEND * usx) / k2)
      const gly = -((uy - usy) / kap - (dl * WIDTH_BLEND * usy) / k2)
      gx += W[m] * inseg * ex + W[m] * (wi[m] - wb) * glx
      gy += W[m] * inseg * ey + W[m] * (wi[m] - wb) * gly
    }
    out.w[q] = wb
    out.gw[2 * q] = gx / Ws
    out.gw[2 * q + 1] = gy / Ws
    out.d[q] = best
    out.gd[2 * q] = usx
    out.gd[2 * q + 1] = usy
  }
  return out
}

/** The LOCALLY capped round-edge radius β = min(b, max(w, d)) at points P and ∇β (heightfield.rim_radius): w, d from
 *  the nearest sample segment when P is closer to it than w/4 (the other side's feet weigh < e^-10), else blendWidth. */
export function rimRadius(ol: OutlineQuery, wv: ArrayLike<number>, P: ArrayLike<number>, bevel: number): { beta: Float64Array; grad: Float64Array } {
  const n = P.length >> 1
  const beta = new Float64Array(n)
  const grad = new Float64Array(2 * n)
  if (!n) return { beta, grad }
  const S = ol.sample
  const b = bevel
  const r = nearest(P, S)
  const w = new Float64Array(n)
  const gw = new Float64Array(2 * n)
  const d = r.d
  const gd = r.g
  const far: number[] = []
  for (let q = 0; q < n; q++) {
    const k = r.seg[q]
    const ex = S.bx[k] - S.ax[k]
    const ey = S.by[k] - S.ay[k]
    const L2 = Math.max(ex * ex + ey * ey, 1e-300)
    const traw = ((P[2 * q] - S.ax[k]) * ex + (P[2 * q + 1] - S.ay[k]) * ey) / L2
    const w0 = wv[k]
    const w1 = wv[S.next[k]]
    w[q] = w0 + Math.min(Math.max(traw, 0), 1) * (w1 - w0)
    const sl = traw > 0 && traw < 1 ? (w1 - w0) / L2 : 0
    gw[2 * q] = sl * ex
    gw[2 * q + 1] = sl * ey
    if (d[q] >= 0.25 * w[q]) far.push(q)
  }
  if (far.length) {
    const Pf = new Float64Array(2 * far.length)
    far.forEach((q, m) => {
      Pf[2 * m] = P[2 * q]
      Pf[2 * m + 1] = P[2 * q + 1]
    })
    const bw = blendWidth(ol, wv, Pf)
    far.forEach((q, m) => {
      w[q] = bw.w[m]
      gw[2 * q] = bw.gw[2 * m]
      gw[2 * q + 1] = bw.gw[2 * m + 1]
      d[q] = bw.d[m]
      gd[2 * q] = bw.gd[2 * m]
      gd[2 * q + 1] = bw.gd[2 * m + 1]
    })
  }
  for (let q = 0; q < n; q++) {
    beta[q] = Math.min(b, Math.max(w[q], d[q]))
    if (w[q] >= b) continue
    const src = w[q] >= d[q] ? gw : gd
    grad[2 * q] = src[2 * q]
    grad[2 * q + 1] = src[2 * q + 1]
  }
  return { beta, grad }
}

// ================================================================================================ Steiner points
function median(v: number[]): number {
  const s = [...v].sort((a, b) => a - b)
  const n = s.length
  return n % 2 ? s[(n - 1) >> 1] : (s[n / 2 - 1] + s[n / 2]) / 2
}

/** Grid-thinning: indices of the first point per cell (in input order). */
function firstPerCell(pts: number[], cell: number): number[] {
  const seen = new Set<string>()
  const out: number[] = []
  const c = Math.max(cell, 1e-12)
  for (let i = 0; i < pts.length >> 1; i++) {
    const key = `${Math.floor(pts[2 * i] / c)},${Math.floor(pts[2 * i + 1] / c)}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(i)
  }
  return out
}

/** For each point of P: is some point of Q closer than its rad? */
function nearAny(P: number[], Q: number[], rad: number[]): boolean[] {
  const nq = Q.length >> 1
  let cell = 1e-12
  for (const r of rad) cell = Math.max(cell, r)
  const grid = new Map<string, number[]>()
  for (let i = 0; i < nq; i++) {
    const key = `${Math.floor(Q[2 * i] / cell)},${Math.floor(Q[2 * i + 1] / cell)}`
    let l = grid.get(key)
    if (!l) grid.set(key, (l = []))
    l.push(i)
  }
  const out: boolean[] = []
  for (let i = 0; i < P.length >> 1; i++) {
    const kx = Math.floor(P[2 * i] / cell)
    const ky = Math.floor(P[2 * i + 1] / cell)
    let hit = false
    for (let dx = -1; dx <= 1 && !hit; dx++)
      for (let dy = -1; dy <= 1 && !hit; dy++) {
        const l = grid.get(`${kx + dx},${ky + dy}`)
        if (!l) continue
        for (const j of l) {
          const ex = Q[2 * j] - P[2 * i]
          const ey = Q[2 * j + 1] - P[2 * i + 1]
          if (ex * ex + ey * ey < rad[i] * rad[i]) {
            hit = true
            break
          }
        }
      }
    out.push(hit)
  }
  return out
}

/** heightfield._thin_grid: the first point per grid cell, per-point cell sizes rounded DOWN to powers of two (each size
 *  class thinned on its own grid). */
export function thinGrid(P: number[], cell: number[]): number[] {
  const seen = new Set<string>()
  const out: number[] = []
  for (let i = 0; i < P.length >> 1; i++) {
    const lc = Math.floor(Math.log2(Math.max(cell[i], 1e-12)))
    const cs = 2 ** lc
    const key = `${lc},${Math.floor(P[2 * i] / cs)},${Math.floor(P[2 * i + 1] / cs)}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(P[2 * i], P[2 * i + 1])
  }
  return out
}

/** heightfield._greedy_thin: drop each point closer than its `rad` to an earlier KEPT point (hash grid, cell = max rad). */
export function greedyThin(P: number[], rad: number[]): number[] {
  const n = P.length >> 1
  if (n < 2) return P.slice()
  let cell = 1e-12
  for (const r of rad) cell = Math.max(cell, r)
  const grid = new Map<string, number[]>()
  const out: number[] = []
  for (let i = 0; i < n; i++) {
    const x = P[2 * i]
    const y = P[2 * i + 1]
    const cx = Math.floor(x / cell)
    const cy = Math.floor(y / cell)
    const r2 = rad[i] * rad[i]
    let hit = false
    for (let ddx = -1; ddx <= 1 && !hit; ddx++)
      for (let ddy = -1; ddy <= 1 && !hit; ddy++) {
        const l = grid.get(`${cx + ddx},${cy + ddy}`)
        if (!l) continue
        for (let m = 0; m < l.length; m += 2)
          if ((l[m] - x) ** 2 + (l[m + 1] - y) ** 2 < r2) {
            hit = true
            break
          }
      }
    if (hit) continue
    const key = `${cx},${cy}`
    let l = grid.get(key)
    if (!l) grid.set(key, (l = []))
    l.push(x, y)
    out.push(x, y)
  }
  return out
}

/**
 * Dome sampling of an inflated island (heightfield._dome_rows, SAMPLING 3b): per ray its MEDIAL distance t_m (BISECT
 * bisection steps on "true distance ≥ (1 − RING_TOL) × distance along the ray", bracketed by its last valid / first
 * failing round-edge ring) and rows at t_m·(1 − cos(jπ/2J)), j = 1..J−1, J = clamp(segments, 3, 12) — the disc model's
 * dome rings scaled to the LOCAL width, so thin parts get as many rows across as discs (the Poisson dome is a round
 * tube there) — plus the medial point at t_m. A row closer than 0.35 × its gap to a valid round-edge ring of the same
 * ray is dropped; rows are thinned per size class on grids of cell max(0.45·min(gap, maxEdge), 0.8·s(x)), s the
 * slope-based spacing of the disc model of radius t_m; medial points are dropped within 0.35·min(t_m − last row,
 * maxEdge) of a kept point and thinned greedily. `sel` = the island's rays (indices into ox / oy / dx / dy / sc).
 */
function domeRows(
  ol: OutlineQuery,
  sel: number[],
  ox: number[],
  oy: number[],
  dx: number[],
  dy: number[],
  sc: number[],
  ds: number[],
  firstBad: Int32Array,
  D: number,
  bevel: number,
  inflate: number,
  segments: number,
  maxEdge: number,
  tanMin: number,
  scaleW: number,
  kept: number[],
): number[] {
  const R = sel.length
  if (!R || !(D > 0)) return []
  const K = ds.length
  const hiAll = (D / (1 - RING_TOL)) * 1.001
  const lo = new Float64Array(R)
  const hi = new Float64Array(R)
  for (let a = 0; a < R; a++) {
    const fb = firstBad[a]
    lo[a] = fb > 0 && K ? ds[fb - 1] : 0
    hi[a] = fb < K ? ds[Math.min(fb, K - 1)] : hiAll
  }
  const mid = new Float64Array(R)
  const P = new Float64Array(2 * R)
  for (let it = 0; it < BISECT; it++) {
    for (let a = 0; a < R; a++) {
      const ri = sel[a]
      mid[a] = 0.5 * (lo[a] + hi[a])
      P[2 * a] = ox[ri] + dx[ri] * sc[ri] * mid[a]
      P[2 * a + 1] = oy[ri] + dy[ri] * sc[ri] * mid[a]
    }
    const dm = nearest(P, ol.shape).d // shape rings: ample for a bracket
    for (let a = 0; a < R; a++) {
      if (dm[a] >= (1 - RING_TOL) * mid[a]) lo[a] = mid[a]
      else hi[a] = mid[a]
    }
  }
  const tm = lo
  const J = Math.max(3, Math.min(12, Math.trunc(segments)))
  const fr: number[] = []
  for (let j = 1; j < J; j++) fr.push(1 - Math.cos((j * Math.PI) / (2 * J)))
  const rows: number[] = []
  for (let j = 0; j < J - 1; j++) {
    const pts: number[] = []
    const cells: number[] = []
    const frPrev = j ? fr[j - 1] : 0
    for (let a = 0; a < R; a++) {
      const t = tm[a]
      const X = t * fr[j]
      if (!(t > 0) || !(X > 0)) continue
      const gap = t * (fr[j] - frPrev)
      let close = false
      for (let r = 0; r < K && r < firstBad[a]; r++)
        if (Math.abs(X - ds[r]) < 0.35 * gap) {
          close = true
          break
        }
      if (close) continue
      const sl = slope(X, bevel, inflate, Math.max(t, 1e-12))
      const tan = Math.min(Math.max((TAN_K * scaleW) / Math.sqrt(Math.max(sl, 1e-6)), tanMin), TAN_MAX * scaleW)
      const ri = sel[a]
      pts.push(ox[ri] + dx[ri] * sc[ri] * X, oy[ri] + dy[ri] * sc[ri] * X)
      cells.push(Math.max(0.45 * Math.min(gap, maxEdge), 0.8 * tan))
    }
    for (const v of thinGrid(pts, cells)) rows.push(v)
  }
  // medial points (the spine of thin parts, the apex of round ones)
  let med: number[] = []
  let rad: number[] = []
  const lastFr = fr[fr.length - 1]
  for (let a = 0; a < R; a++) {
    const t = tm[a]
    if (!(t > 1e-9)) continue
    const ri = sel[a]
    med.push(ox[ri] + dx[ri] * sc[ri] * t, oy[ri] + dy[ri] * sc[ri] * t)
    rad.push(0.35 * Math.min(t * (1 - lastFr), maxEdge))
  }
  const allk = kept.concat(rows)
  if (med.length && allk.length) {
    const hit = nearAny(med, allk, rad)
    const m2: number[] = []
    const r2: number[] = []
    for (let i = 0; i < rad.length; i++)
      if (!hit[i]) {
        m2.push(med[2 * i], med[2 * i + 1])
        r2.push(rad[i])
      }
    med = m2
    rad = r2
  }
  if (med.length) med = greedyThin(med, rad)
  return rows.concat(med)
}

/** Graded ring points + medial points (SAMPLING 2–3 of heightfield.py; inflated islands: dome rows, 3b). Flat x,y pairs. */
export function steinerPoints(
  ol: OutlineQuery,
  Dr: Float64Array,
  bevel: number,
  inflate: number,
  segments: number,
  maxEdge: number,
  tanMin = 0,
  scaleW = 1,
): number[] {
  const { rings, island } = ol.o
  // rays: origin, direction, distance scale, island
  const ox: number[] = []
  const oy: number[] = []
  const dx: number[] = []
  const dy: number[] = []
  const sc: number[] = []
  const isl: number[] = []
  const fanStep = (FAN_DEG * Math.PI) / 180
  rings.forEach((r, k) => {
    const n = r.length >> 1
    for (let i = 0; i < n; i++) {
      const p = (i - 1 + n) % n
      const q = (i + 1) % n
      let tix = r[2 * i] - r[2 * p]
      let tiy = r[2 * i + 1] - r[2 * p + 1]
      let l = Math.max(Math.hypot(tix, tiy), 1e-300)
      tix /= l
      tiy /= l
      let tox = r[2 * q] - r[2 * i]
      let toy = r[2 * q + 1] - r[2 * i + 1]
      l = Math.max(Math.hypot(tox, toy), 1e-300)
      tox /= l
      toy /= l
      const ninx = -tiy
      const niny = tix
      const noutx = -toy
      const nouty = tox
      const alpha = Math.atan2(tix * toy - tiy * tox, tix * tox + tiy * toy) // > 0: convex
      let bx = ninx + noutx
      let by = niny + nouty
      const bn = Math.hypot(bx, by)
      if (bn > 1e-6) {
        bx /= bn
        by /= bn
      } else if (alpha > 0) {
        bx = -tix
        by = -tiy
      } else {
        bx = tix
        by = tiy
      }
      ox.push(r[2 * i])
      oy.push(r[2 * i + 1])
      dx.push(bx)
      dy.push(by)
      sc.push(alpha > 0 ? 1 / Math.max(Math.cos(alpha / 2), 0.1) : 1)
      isl.push(island[k])
      if (alpha < -fanStep) {
        const J = Math.ceil(Math.abs(alpha) / fanStep)
        for (let s = 1; s < J; s++) {
          const ang = (alpha * s) / J
          const c = Math.cos(ang)
          const sn = Math.sin(ang)
          ox.push(r[2 * i])
          oy.push(r[2 * i + 1])
          dx.push(c * ninx - sn * niny)
          dy.push(sn * ninx + c * niny)
          sc.push(1)
          isl.push(island[k])
        }
      }
    }
  })
  const out: number[] = []
  const islands = [...new Set(isl)].sort((a, b) => a - b)
  const dome = inflate > 0
  for (const I of islands) {
    const sel: number[] = []
    isl.forEach((v, i) => v === I && sel.push(i))
    const D = Dr[I]
    // inflated bodies: global rings for the round edge only; the dome gets per-ray rows (SAMPLING 3b, domeRows).
    // flat bodies: the round edge's rings at the island's capped radius min(b, D) (round 8: an island narrower than the
    // bevel is a round tube / sphere of radius D — rings graded to b stopped short of its spine)
    const kRing = dome ? 0 : inflate
    const ds = ringDistances(dome ? bevel : Math.min(bevel, D), kRing, D, segments)
    const K = ds.length
    if (!K && !dome) continue
    const Rn = sel.length
    const P = new Float64Array(Rn * K * 2)
    sel.forEach((ri, a) => {
      for (let j = 0; j < K; j++) {
        const t = sc[ri] * ds[j]
        P[(a * K + j) * 2] = ox[ri] + dx[ri] * t
        P[(a * K + j) * 2 + 1] = oy[ri] + dy[ri] * t
      }
    })
    const dtrue = K ? ol.query(P).d : new Float64Array(0)
    const firstBad = new Int32Array(Rn)
    for (let a = 0; a < Rn; a++) {
      let j = 0
      while (j < K && dtrue[a * K + j] >= (1 - RING_TOL) * ds[j]) j++
      firstBad[a] = j
    }
    const gaps = ds.map((x, i) => x - (i ? ds[i - 1] : 0))
    const kept: number[] = []
    for (let j = 0; j < K; j++) {
      const sl = slope(ds[j], bevel, kRing, D)
      const tan = Math.min(Math.max((TAN_K * scaleW) / Math.sqrt(Math.max(sl, 1e-6)), tanMin), TAN_MAX * scaleW)
      const q: number[] = []
      for (let a = 0; a < Rn; a++) if (firstBad[a] > j) q.push(P[(a * K + j) * 2], P[(a * K + j) * 2 + 1])
      if (!q.length) continue
      const cellr = Math.max(0.45 * Math.min(gaps[j], maxEdge), 0.8 * tan)
      for (const i of firstPerCell(q, cellr)) kept.push(q[2 * i], q[2 * i + 1])
    }
    if (dome) {
      const rows = domeRows(ol, sel, ox, oy, dx, dy, sc, ds, firstBad, D, bevel, inflate, segments, maxEdge, tanMin, scaleW, kept)
      for (const v of kept) out.push(v)
      for (const v of rows) out.push(v)
      continue
    }
    // medial points: ON the ray's medial crossing — BISECT bisection steps between the last valid ring (or the outline
    // vertex) and the first failure (round 8: the spine of a thin part is the top of its round tube; halfway between the
    // two rings left it 20-40 % short and the tube's top a coarse fold)
    let med: number[] = []
    let rad: number[] = []
    const rr: number[] = []
    for (let a = 0; a < Rn; a++) if (firstBad[a] < K) rr.push(a)
    if (rr.length) {
      const loT = new Float64Array(rr.length)
      const hiT = new Float64Array(rr.length)
      rr.forEach((a, m) => {
        const fb = firstBad[a]
        loT[m] = fb > 0 ? ds[fb - 1] : 0
        hiT[m] = ds[fb]
      })
      const Q = new Float64Array(2 * rr.length)
      const mid = new Float64Array(rr.length)
      for (let it = 0; it < BISECT; it++) {
        rr.forEach((a, m) => {
          const ri = sel[a]
          mid[m] = 0.5 * (loT[m] + hiT[m])
          Q[2 * m] = ox[ri] + dx[ri] * sc[ri] * mid[m]
          Q[2 * m + 1] = oy[ri] + dy[ri] * sc[ri] * mid[m]
        })
        const dq = ol.query(Q).d
        for (let m = 0; m < rr.length; m++) {
          if (dq[m] >= (1 - RING_TOL) * mid[m]) loT[m] = mid[m]
          else hiT[m] = mid[m]
        }
      }
      rr.forEach((a, m) => {
        const ri = sel[a]
        med.push(ox[ri] + dx[ri] * sc[ri] * loT[m], oy[ri] + dy[ri] * sc[ri] * loT[m])
        const dmed = Math.max(loT[m], 0.5 * ds[0])
        let si = 0
        while (si < K && ds[si] < dmed) si++
        rad.push(0.35 * Math.min(gaps[Math.min(si, K - 1)], maxEdge))
      })
    }
    if (med.length) {
      const first = firstPerCell(med, 2 * median(rad))
      med = first.flatMap((i) => [med[2 * i], med[2 * i + 1]])
      rad = first.map((i) => rad[i])
      if (kept.length && med.length) {
        const hit = nearAny(med, kept, rad)
        med = med.filter((_, i) => !hit[i >> 1])
      }
      if (med.length) {
        const dm = ol.query(med).d
        for (let i = 0; i < dm.length; i++) if (dm[i] > 0.25 * ds[0]) kept.push(med[2 * i], med[2 * i + 1])
      }
    }
    for (const v of kept) out.push(v)
  }
  return out
}

/** Steiner points + the apex (inradius centre) of every island whose profile still rises at D (inflate, or D < b),
 * unless a Steiner point already lies within APEX_CLEAR × (D − its last ring distance). */
export function withApices(
  steiner: number[],
  centres: Map<number, P2>,
  Dr: Float64Array,
  bevel: number,
  inflate: number,
  segments: number,
): number[] {
  const out = steiner.slice()
  for (const [q, c] of centres) {
    const D = Dr[q]
    if (D <= 0 || !(inflate > 0 || D < bevel)) continue
    const ds = ringDistances(inflate > 0 ? bevel : Math.min(bevel, D), inflate, D, segments)
    const clear = APEX_CLEAR * (D - (ds.length ? ds[ds.length - 1] : 0))
    let near = false
    for (let i = 0; i < steiner.length && !near; i += 2) if (Math.hypot(steiner[i] - c[0], steiner[i + 1] - c[1]) < clear) near = true
    if (!near) out.push(c[0], c[1])
  }
  return out
}

/** Drop points within about q of an earlier point (or a fixed point): rounding to a q-grid and a half-cell shifted grid. */
export function mergePoints(P: number[], q: number, fixed: number[] = []): number[] {
  const n = P.length >> 1
  const keep = new Uint8Array(n).fill(1)
  for (const off of [0, 0.5]) {
    const seen = new Set<string>()
    const key = (x: number, y: number) => `${Math.round(x / q + off)},${Math.round(y / q + off)}`
    for (let i = 0; i < fixed.length; i += 2) seen.add(key(fixed[i], fixed[i + 1]))
    for (let i = 0; i < n; i++) {
      if (!keep[i]) continue
      const k = key(P[2 * i], P[2 * i + 1])
      if (seen.has(k)) keep[i] = 0
      else seen.add(k)
    }
  }
  const out: number[] = []
  for (let i = 0; i < n; i++) if (keep[i]) out.push(P[2 * i], P[2 * i + 1])
  return out
}

// ================================================================================================ triangulation
interface TriPoint {
  x: number
  y: number
  i: number
}

export interface Triangulation {
  /** Vertex positions (flat x,y): outline vertices first (in ring order), then Steiner points. */
  V: number[]
  /** Triangles (vertex index triplets), CCW. */
  T: number[]
  /** Number of outline vertices (indices < nOutline are on the outline). */
  nOutline: number
  /** Islands that needed a fallback (poly2tri failure). */
  fallbacks: number
}

function islandOf(ol: OutlineQuery, x: number, y: number, guess: number, groups: Map<number, number[]>): number {
  const inIsland = (I: number) => {
    const rs = groups.get(I)
    if (!rs) return false
    if (!pointInRing(x, y, ol.o.rings[rs[0]])) return false
    for (let m = 1; m < rs.length; m++) if (pointInRing(x, y, ol.o.rings[rs[m]])) return false
    return true
  }
  if (inIsland(guess)) return guess
  for (const I of groups.keys()) if (I !== guess && inIsland(I)) return I
  return -1
}

/**
 * Deterministic sub-tolerance jitter (JITTER local units) of the triangulator's INPUT copy of every point: poly2tri
 * rejects exactly collinear constraint points ("EdgeEvent: Collinear not supported!" — straight runs subdivided by
 * MAX_EDGE, axis-aligned edges). The mesh keeps the exact positions (poly2tri only returns indices).
 */
export const JITTER = 1e-8
/** × JITTER of the last poly2tri attempt before the ear-clipping fallback: a vertex of one ring ON another ring's edge
 *  (or a Steiner point on it) stays "collinear" for poly2tri's 1e-12 orientation test at 1e-8 — the island then fell back
 *  to a FLAT ear-clipped body while the worker's CDT domes it (Home's silhouette and Scandit's dot at layer scale 0.7,
 *  Recorder / Translate pieces at 1.3: 61 of the corpus' 97 such builds now triangulate). */
export const JITTER_RETRY = 100
function jit(i: number, axis: number, scale = 1): number {
  const s = Math.sin(i * 12.9898 + axis * 78.233) * 43758.5453
  return (s - Math.floor(s) - 0.5) * 2 * JITTER * scale
}

/** Relative size (× the island's extent) of a touch between rings, and of the separating nudge (see separateHoles). */
const TOUCH_REL = 1e-6
const NUDGE_REL = 4e-6

/**
 * Triangulator-input offsets that pull every HOLE vertex touching another ring of its island (a shared vertex, or a
 * hole edge lying on the outer / another hole: pathops output can do both) NUDGE_REL × the island's extent into its
 * own hole. poly2tri rejects coincident / overlapping constraints; mathutils' CDT merges them. The gap left is a
 * sliver at d ≈ 0 (wall height), far below a pixel; the mesh keeps the exact positions (indices only).
 */
function separateHoles(rings: Ring[], ringIdx: number[]): Map<number, P2> {
  const off = new Map<number, P2>()
  if (ringIdx.length < 2) return off
  const [x0, y0, x1, y1] = ringBox(rings[ringIdx[0]])
  const ext = Math.max(x1 - x0, y1 - y0, 1e-12)
  const touch = TOUCH_REL * ext
  const nudge = NUDGE_REL * ext
  const near = (x: number, y: number, k: number) => {
    const r = rings[k]
    const n = r.length >> 1
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n
      const ax = r[2 * i]
      const ay = r[2 * i + 1]
      const dx = r[2 * j] - ax
      const dy = r[2 * j + 1] - ay
      const l2 = dx * dx + dy * dy
      const t = l2 > 0 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / l2)) : 0
      if (Math.hypot(x - ax - t * dx, y - ay - t * dy) <= touch) return true
    }
    return false
  }
  for (let m = 1; m < ringIdx.length; m++) {
    const k = ringIdx[m]
    const r = rings[k]
    const n = r.length >> 1
    const into = ringArea(r) > 0 ? 1 : -1 // the hole's own interior: left of a CCW ring, right of a CW one
    for (let i = 0; i < n; i++) {
      const x = r[2 * i]
      const y = r[2 * i + 1]
      if (!ringIdx.some((q) => q !== k && near(x, y, q))) continue
      const p = (i + n - 1) % n
      const q = (i + 1) % n
      const e1x = x - r[2 * p]
      const e1y = y - r[2 * p + 1]
      const e2x = r[2 * q] - x
      const e2y = r[2 * q + 1] - y
      const l1 = Math.hypot(e1x, e1y) || 1
      const l2 = Math.hypot(e2x, e2y) || 1
      // left normals of the two edges, averaged → toward the hole's interior (× into)
      let nx = into * (-e1y / l1 - e2y / l2)
      let ny = into * (e1x / l1 + e2x / l2)
      const nl = Math.hypot(nx, ny)
      if (nl < 1e-12) {
        nx = into * (-e2y / l2)
        ny = into * (e2x / l2)
      } else {
        nx /= nl
        ny /= nl
      }
      off.set(k * 0x100000 + i, [nudge * nx, nudge * ny])
    }
  }
  return off
}

function triangulateIsland(
  ol: OutlineQuery,
  ringIdx: number[],
  starts: number[],
  steiner: TriPoint[],
  separate = false,
  jscale = 1,
): number[] | null {
  const rings = ol.o.rings
  const off = separate ? separateHoles(rings, ringIdx) : null
  const contour = (k: number): TriPoint[] => {
    const r = rings[k]
    const n = r.length >> 1
    const pts: TriPoint[] = []
    for (let i = 0; i < n; i++) {
      const id = starts[k] + i
      const o = off?.get(k * 0x100000 + i)
      pts.push({ x: r[2 * i] + jit(id, 0, jscale) + (o ? o[0] : 0), y: r[2 * i + 1] + jit(id, 1, jscale) + (o ? o[1] : 0), i: id })
    }
    return pts
  }
  const ctx = new poly2tri.SweepContext(contour(ringIdx[0]), { cloneArrays: false })
  for (let m = 1; m < ringIdx.length; m++) ctx.addHole(contour(ringIdx[m]))
  if (steiner.length) ctx.addPoints(steiner.map((p) => ({ x: p.x + jit(p.i, 0, jscale), y: p.y + jit(p.i, 1, jscale), i: p.i })))
  ctx.triangulate()
  const T: number[] = []
  for (const t of ctx.getTriangles()) {
    const a = t.getPoint(0) as unknown as TriPoint
    const b = t.getPoint(1) as unknown as TriPoint
    const c = t.getPoint(2) as unknown as TriPoint
    // CCW in the triangulator's own (jittered) coordinates: exact-coordinate slivers keep a consistent winding
    const ar = (b.x - a.x) * (c.y - a.y) - (c.x - a.x) * (b.y - a.y)
    if (ar < 0) T.push(a.i, c.i, b.i)
    else T.push(a.i, b.i, c.i)
  }
  return T
}

/** Earcut fallback (no Steiner points) for an island poly2tri cannot triangulate (touching rings). */
function earcutIsland(ol: OutlineQuery, ringIdx: number[], starts: number[]): number[] {
  // THREE.ShapeUtils lives in three; a tiny ear-clipping import keeps this module three-free for node tests.
  const rings = ringIdx.map((k) => ol.o.rings[k])
  const flat: number[] = []
  const holes: number[] = []
  const map: number[] = []
  rings.forEach((r, m) => {
    if (m) holes.push(flat.length >> 1)
    for (let i = 0; i < r.length >> 1; i++) {
      flat.push(r[2 * i], r[2 * i + 1])
      map.push(starts[ringIdx[m]] + i)
    }
  })
  return earcut(flat, holes).map((i) => map[i])
}

/**
 * CDT of the outline (rings) + Steiner points, one poly2tri sweep per island. Steiner points are assigned to the island
 * that contains them; an island poly2tri rejects is retried without its Steiner points closest to the outline, with its
 * touching holes nudged apart, with a coarser input jitter (JITTER_RETRY), then ear-clipped without any (flat).
 */
export function triangulate(ol: OutlineQuery, steiner: number[]): Triangulation {
  const rings = ol.o.rings
  const starts: number[] = []
  const V: number[] = []
  for (const r of rings) {
    starts.push(V.length >> 1)
    for (const v of r) V.push(v)
  }
  const nOutline = V.length >> 1
  const groups = new Map<number, number[]>()
  ol.o.island.forEach((I, k) => {
    let g = groups.get(I)
    if (!g) groups.set(I, (g = []))
    if (k === I) g.unshift(k)
    else g.push(k)
  })
  // islands whose first ring is not their outer (malformed nesting) are dropped
  for (const [I, g] of groups) if (g[0] !== I) groups.delete(I)
  const per = new Map<number, TriPoint[]>()
  if (steiner.length) {
    const guess = ol.query(steiner).isl
    for (let s = 0; s < steiner.length >> 1; s++) {
      const x = steiner[2 * s]
      const y = steiner[2 * s + 1]
      const I = islandOf(ol, x, y, guess[s], groups)
      if (I < 0) continue
      let l = per.get(I)
      if (!l) per.set(I, (l = []))
      l.push({ x, y, i: V.length >> 1 })
      V.push(x, y)
    }
  }
  const T: number[] = []
  let fallbacks = 0
  for (const [I, ringIdx] of groups) {
    const pts = per.get(I) ?? []
    let tri: number[] | null = null
    try {
      tri = triangulateIsland(ol, ringIdx, starts, pts)
    } catch {
      tri = null
    }
    let safe: TriPoint[] | null = null
    if (!tri && pts.length) {
      // drop Steiner points hugging the outline (poly2tri: points on / next to constraint edges)
      const d = ol.query(pts.flatMap((p) => [p.x, p.y])).d
      let lim = 0
      for (const v of d) lim = Math.max(lim, v)
      safe = pts.filter((_, i) => d[i] > 1e-3 * lim).map((p) => ({ x: p.x, y: p.y, i: p.i }))
      try {
        tri = triangulateIsland(ol, ringIdx, starts, safe)
      } catch {
        tri = null
      }
    }
    // rings touching each other (a hole on the outer / on another hole): separate them by a sub-pixel nudge
    for (const sp of [pts, safe]) {
      if (tri || !sp || ringIdx.length < 2) continue
      try {
        tri = triangulateIsland(ol, ringIdx, starts, sp, true)
      } catch {
        tri = null
      }
    }
    // a point ON another ring's edge (a T-junction separateHoles does not nudge): a coarser jitter (JITTER_RETRY)
    for (const sep of [false, true]) {
      if (tri || (sep && ringIdx.length < 2)) continue
      try {
        tri = triangulateIsland(ol, ringIdx, starts, safe ?? pts, sep, JITTER_RETRY)
      } catch {
        tri = null
      }
    }
    if (!tri) {
      fallbacks++
      try {
        tri = earcutIsland(ol, ringIdx, starts)
      } catch {
        tri = []
      }
      // CCW (earcut's winding follows its input)
      for (let t = 0; t < tri.length; t += 3) {
        const a = tri[t]
        const b = tri[t + 1]
        const c = tri[t + 2]
        const ar = (V[2 * b] - V[2 * a]) * (V[2 * c + 1] - V[2 * a + 1]) - (V[2 * c] - V[2 * a]) * (V[2 * b + 1] - V[2 * a + 1])
        if (ar < 0) {
          tri[t + 1] = c
          tri[t + 2] = b
        }
      }
    }
    for (const v of tri) T.push(v)
  }
  const cons = new Set<number>()
  rings.forEach((r, k) => {
    const n = r.length >> 1
    for (let i = 0; i < n; i++) {
      const a = starts[k] + i
      const b = starts[k] + ((i + 1) % n)
      cons.add(a < b ? a * nOutline + b : b * nOutline + a)
    }
  })
  delaunayFlips(V, T, (a, b) => a < nOutline && b < nOutline && cons.has(a < b ? a * nOutline + b : b * nOutline + a))
  return { V, T, nOutline, fallbacks }
}

/**
 * Lawson edge flips until every unconstrained interior edge is locally Delaunay (empty circumcircle). poly2tri's sweep
 * leaves a few non-Delaunay slivers between the graded rings (a ring vertex joined to a far vertex across a flat cap:
 * its tilted normal then smeared across the cap as streaks); mathutils' CDT is exactly Delaunay.
 */
export function delaunayFlips(V: ArrayLike<number>, T: number[], constrained: (a: number, b: number) => boolean): number {
  const nt = T.length / 3
  if (!nt) return 0
  let ext = 0
  for (let i = 0; i < T.length; i++) ext = Math.max(ext, Math.abs(V[2 * T[i]]), Math.abs(V[2 * T[i] + 1]))
  const eps = 1e-13 * Math.max(ext, 1e-6) ** 4
  const N = V.length >> 1
  const he = new Map<number, number>() // directed edge a→b → triangle
  const key = (a: number, b: number) => a * N + b
  for (let t = 0; t < nt; t++) for (let c = 0; c < 3; c++) he.set(key(T[3 * t + c], T[3 * t + ((c + 1) % 3)]), t)
  const orient = (a: number, b: number, c: number) =>
    (V[2 * b] - V[2 * a]) * (V[2 * c + 1] - V[2 * a + 1]) - (V[2 * c] - V[2 * a]) * (V[2 * b + 1] - V[2 * a + 1])
  const incircle = (a: number, b: number, c: number, d: number) => {
    const ax = V[2 * a] - V[2 * d]
    const ay = V[2 * a + 1] - V[2 * d + 1]
    const bx = V[2 * b] - V[2 * d]
    const by = V[2 * b + 1] - V[2 * d + 1]
    const cx = V[2 * c] - V[2 * d]
    const cy = V[2 * c + 1] - V[2 * d + 1]
    return (ax * ax + ay * ay) * (bx * cy - cx * by) - (bx * bx + by * by) * (ax * cy - cx * ay) + (cx * cx + cy * cy) * (ax * by - bx * ay)
  }
  const third = (t: number, a: number, b: number) => {
    for (let c = 0; c < 3; c++) {
      const v = T[3 * t + c]
      if (v !== a && v !== b) return v
    }
    return -1
  }
  const stack: number[] = []
  for (let t = 0; t < nt; t++)
    for (let c = 0; c < 3; c++) {
      const a = T[3 * t + c]
      const b = T[3 * t + ((c + 1) % 3)]
      if (a < b) stack.push(a, b)
    }
  let flips = 0
  const cap = 40 * nt
  while (stack.length && flips < cap) {
    const b = stack.pop()!
    const a = stack.pop()!
    const t1 = he.get(key(a, b))
    const t2 = he.get(key(b, a))
    if (t1 === undefined || t2 === undefined || t1 === t2 || constrained(a, b)) continue
    const c = third(t1, a, b) // t1 = (a, b, c) CCW
    const d = third(t2, a, b) // t2 = (b, a, d) CCW
    if (c < 0 || d < 0 || c === d) continue
    if (incircle(a, b, c, d) <= eps) continue
    // the flipped pair (a, d, c) + (d, b, c) must stay CCW (convex quadrilateral)
    if (orient(a, d, c) <= 0 || orient(d, b, c) <= 0) continue
    T[3 * t1] = a
    T[3 * t1 + 1] = d
    T[3 * t1 + 2] = c
    T[3 * t2] = d
    T[3 * t2 + 1] = b
    T[3 * t2 + 2] = c
    he.delete(key(a, b))
    he.delete(key(b, a))
    he.set(key(a, d), t1)
    he.set(key(d, c), t1)
    he.set(key(c, a), t1)
    he.set(key(d, b), t2)
    he.set(key(b, c), t2)
    he.set(key(c, d), t2)
    stack.push(a, d, d, b, b, c, c, a)
    flips++
  }
  return flips
}

// ------------------------------------------------------------------------------------------------ ear clipping
// Compact earcut (mapbox/earcut algorithm, ISC) — only used as the last-resort fallback triangulator.
function earcut(data: number[], holeIndices: number[]): number[] {
  type N = { i: number; x: number; y: number; prev: N; next: N; steiner: boolean }
  const node = (i: number, x: number, y: number): N => {
    const n = { i, x, y, steiner: false } as N
    n.prev = n
    n.next = n
    return n
  }
  const insert = (i: number, x: number, y: number, last: N | null): N => {
    const p = node(i, x, y)
    if (!last) {
      p.prev = p
      p.next = p
    } else {
      p.next = last.next
      p.prev = last
      last.next.prev = p
      last.next = p
    }
    return p
  }
  const remove = (p: N) => {
    p.next.prev = p.prev
    p.prev.next = p.next
  }
  const area = (p: N, q: N, r: N) => (q.y - p.y) * (r.x - q.x) - (q.x - p.x) * (r.y - q.y)
  const equals = (p: N, q: N) => p.x === q.x && p.y === q.y
  const signedArea = (start: number, end: number) => {
    let s = 0
    for (let i = start, j = end - 2; i < end; i += 2) {
      s += (data[j] - data[i]) * (data[i + 1] + data[j + 1])
      j = i
    }
    return s
  }
  const linked = (start: number, end: number, clockwise: boolean): N | null => {
    let last: N | null = null
    if (clockwise === signedArea(start, end) > 0) for (let i = start; i < end; i += 2) last = insert(i / 2, data[i], data[i + 1], last)
    else for (let i = end - 2; i >= start; i -= 2) last = insert(i / 2, data[i], data[i + 1], last)
    if (last && equals(last, last.next)) {
      remove(last)
      last = last.next
    }
    return last
  }
  const inTri = (ax: number, ay: number, bx: number, by: number, cx: number, cy: number, px: number, py: number) =>
    (cx - px) * (ay - py) >= (ax - px) * (cy - py) &&
    (ax - px) * (by - py) >= (bx - px) * (ay - py) &&
    (bx - px) * (cy - py) >= (cx - px) * (by - py)
  const isEar = (ear: N) => {
    const a = ear.prev
    const b = ear
    const c = ear.next
    if (area(a, b, c) >= 0) return false
    let p = ear.next.next
    while (p !== ear.prev) {
      if (inTri(a.x, a.y, b.x, b.y, c.x, c.y, p.x, p.y) && area(p.prev, p, p.next) >= 0) return false
      p = p.next
    }
    return true
  }
  const filter = (start: N, end?: N): N => {
    end = end ?? start
    let p = start
    let again: boolean
    do {
      again = false
      if (!p.steiner && (equals(p, p.next) || area(p.prev, p, p.next) === 0)) {
        remove(p)
        p = end = p.prev
        if (p === p.next) break
        again = true
      } else p = p.next
    } while (again || p !== end)
    return end
  }
  const segIntersect = (p1: N, q1: N, p2: N, q2: N) => {
    const o = (p: N, q: N, r: N) => Math.sign(area(p, q, r))
    return o(p1, q1, p2) !== o(p1, q1, q2) && o(p2, q2, p1) !== o(p2, q2, q1)
  }
  const locallyInside = (a: N, b: N) =>
    area(a.prev, a, a.next) < 0 ? area(a, b, a.next) >= 0 && area(a, a.prev, b) >= 0 : area(a, b, a.prev) < 0 || area(a, a.next, b) < 0
  const split = (a: N, b: N): N => {
    const a2 = node(a.i, a.x, a.y)
    const b2 = node(b.i, b.x, b.y)
    const an = a.next
    const bp = b.prev
    a.next = b
    b.prev = a
    a2.next = an
    an.prev = a2
    b2.next = a2
    a2.prev = b2
    bp.next = b2
    b2.prev = bp
    return b2
  }
  const findHoleBridge = (hole: N, outer: N): N | null => {
    let p = outer
    const hx = hole.x
    const hy = hole.y
    let qx = -Infinity
    let m: N | null = null
    do {
      if (hy <= p.y && hy >= p.next.y && p.next.y !== p.y) {
        const x = p.x + ((hy - p.y) * (p.next.x - p.x)) / (p.next.y - p.y)
        if (x <= hx && x > qx) {
          qx = x
          m = p.x < p.next.x ? p : p.next
          if (x === hx) return m
        }
      }
      p = p.next
    } while (p !== outer)
    if (!m) return null
    const stop = m
    const mx = m.x
    const my = m.y
    let tanMin = Infinity
    p = m
    do {
      if (hx >= p.x && p.x >= mx && hx !== p.x && inTri(hy < my ? hx : qx, hy, mx, my, hy < my ? qx : hx, hy, p.x, p.y)) {
        const tan = Math.abs(hy - p.y) / (hx - p.x)
        if (locallyInside(p, hole) && (tan < tanMin || (tan === tanMin && p.x > m!.x))) {
          m = p
          tanMin = tan
        }
      }
      p = p.next
    } while (p !== stop)
    return m
  }
  let outer = linked(0, holeIndices.length ? holeIndices[0] * 2 : data.length, true)
  const tris: number[] = []
  if (!outer || outer.next === outer.prev) return tris
  if (holeIndices.length) {
    const queue: N[] = []
    for (let i = 0; i < holeIndices.length; i++) {
      const s = holeIndices[i] * 2
      const e = i < holeIndices.length - 1 ? holeIndices[i + 1] * 2 : data.length
      const list = linked(s, e, false)
      if (!list) continue
      if (list === list.next) list.steiner = true
      let left = list
      let p = list
      do {
        if (p.x < left.x || (p.x === left.x && p.y < left.y)) left = p
        p = p.next
      } while (p !== list)
      queue.push(left)
    }
    queue.sort((a, b) => a.x - b.x)
    for (const h of queue) {
      const bridge = findHoleBridge(h, outer!)
      if (!bridge) continue
      const b2 = split(bridge, h)
      filter(b2, b2.next)
      outer = filter(bridge, bridge.next)
    }
  }
  let ear: N = outer!
  let stop = ear
  let guard = 0
  while (ear.prev !== ear.next && guard++ < 1e6) {
    const prev = ear.prev
    const next = ear.next
    if (isEar(ear)) {
      tris.push(prev.i, ear.i, next.i)
      remove(ear)
      ear = next.next
      stop = next.next
      continue
    }
    ear = next
    if (ear === stop) {
      // cure local self-intersections, else give up on the remainder
      let p = ear
      let cured = false
      do {
        const a = p.prev
        const b = p.next.next
        if (!equals(a, b) && segIntersect(a, p, p.next, b) && locallyInside(a, b) && locallyInside(b, a)) {
          tris.push(a.i, p.i, b.i)
          remove(p)
          remove(p.next)
          p = b
          cured = true
        }
        p = p.next
      } while (p !== ear)
      if (!cured) break
      ear = p
      stop = p
    }
  }
  return tris
}

// ================================================================================================ Poisson inflation
export interface PoissonInfo {
  iterations: number
  /** |residual| / |load| at the stop. */
  residual: number
  /** Free (interior) vertices. */
  free: number
}

/**
 * heightfield.poisson: solve −∇²u = f on the triangulated region (V flat x,y; CCW triangles T), u = 0 at the `fixed`
 * vertices. Cotangent Laplacian with the circumcentric dual area: K u = b with K_ij = −w_ij, K_ii = Σ_j w_ij, edge weight
 * w_ij = ½(cot α_ij + cot β_ij) (the angles opposite edge ij in its one or two triangles, summed per undirected edge and
 * clamped to ≥ 0) and load b_i = f·Σ_j w_ij·|x_j − x_i|²/4 (quadratics — a disc's R² − r² — come out exact at the
 * vertices). Jacobi-preconditioned conjugate gradients from u = 0 until |r| ≤ tol·|b|; u clamped to ≥ 0.
 */
export function poisson(
  V: ArrayLike<number>,
  T: ArrayLike<number>,
  fixed: ArrayLike<number>,
  f = POISSON_F,
  tol = POISSON_TOL,
  maxIter = POISSON_MAX_ITER,
): { u: Float64Array; info: PoissonInfo } {
  const n = V.length >> 1
  const u = new Float64Array(n)
  let nFree = 0
  for (let i = 0; i < n; i++) if (!fixed[i]) nFree++
  const info: PoissonInfo = { iterations: 0, residual: 0, free: nFree }
  const nt = (T.length / 3) | 0
  if (!nt || !nFree) return { u, info }
  // undirected edges with their summed cotangent weights
  const index = new Map<number, number>()
  const ea: number[] = []
  const eb: number[] = []
  const ew: number[] = []
  const addEdge = (a: number, b: number, w: number) => {
    const lo = a < b ? a : b
    const hi = a < b ? b : a
    const key = lo * n + hi
    const k = index.get(key)
    if (k === undefined) {
      index.set(key, ea.length)
      ea.push(lo)
      eb.push(hi)
      ew.push(w)
    } else ew[k] += w
  }
  for (let t = 0; t < nt; t++) {
    const i0 = T[3 * t]
    const i1 = T[3 * t + 1]
    const i2 = T[3 * t + 2]
    const x0 = V[2 * i0]
    const y0 = V[2 * i0 + 1]
    const x1 = V[2 * i1]
    const y1 = V[2 * i1 + 1]
    const x2 = V[2 * i2]
    const y2 = V[2 * i2 + 1]
    const a2 = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0) // 2 × signed area
    const a2s = Math.abs(a2) > 1e-300 ? a2 : 1e-300
    // cot of the angle at each corner = (its two edge vectors' dot) / (2 × area); weight of the OPPOSITE edge
    const c0 = ((x1 - x0) * (x2 - x0) + (y1 - y0) * (y2 - y0)) / a2s
    const c1 = ((x2 - x1) * (x0 - x1) + (y2 - y1) * (y0 - y1)) / a2s
    const c2 = ((x0 - x2) * (x1 - x2) + (y0 - y2) * (y1 - y2)) / a2s
    addEdge(i1, i2, 0.5 * c0)
    addEdge(i2, i0, 0.5 * c1)
    addEdge(i0, i1, 0.5 * c2)
  }
  const E = ea.length
  const A = Int32Array.from(ea)
  const B = Int32Array.from(eb)
  const W = new Float64Array(E)
  const diag = new Float64Array(n)
  const b = new Float64Array(n)
  for (let e = 0; e < E; e++) {
    const w = Math.max(ew[e], 0)
    W[e] = w
    const i = A[e]
    const j = B[e]
    diag[i] += w
    diag[j] += w
    const L2 = (V[2 * j] - V[2 * i]) ** 2 + (V[2 * j + 1] - V[2 * i + 1]) ** 2
    b[i] += (f / 4) * w * L2
    b[j] += (f / 4) * w * L2
  }
  const ok = new Uint8Array(n)
  const dinv = new Float64Array(n)
  for (let i = 0; i < n; i++) {
    ok[i] = !fixed[i] && diag[i] > 1e-300 ? 1 : 0
    if (!ok[i]) b[i] = 0
    else dinv[i] = 1 / diag[i]
  }
  // K x for x zero at every fixed vertex (p stays zero there: r, z, p start from b)
  const q = new Float64Array(n)
  const K = (x: Float64Array) => {
    for (let i = 0; i < n; i++) q[i] = diag[i] * x[i]
    for (let e = 0; e < E; e++) {
      const i = A[e]
      const j = B[e]
      const w = W[e]
      q[i] -= w * x[j]
      q[j] -= w * x[i]
    }
    for (let i = 0; i < n; i++) if (!ok[i]) q[i] = 0
  }
  let bn = 0
  for (let i = 0; i < n; i++) bn += b[i] * b[i]
  bn = Math.sqrt(bn)
  if (!(bn > 0)) return { u, info }
  const r = Float64Array.from(b)
  const z = new Float64Array(n)
  const p = new Float64Array(n)
  let rz = 0
  for (let i = 0; i < n; i++) {
    z[i] = dinv[i] * r[i]
    p[i] = z[i]
    rz += r[i] * z[i]
  }
  let it = 0
  let rn = bn
  for (it = 1; it <= maxIter; it++) {
    K(p)
    let pq = 0
    for (let i = 0; i < n; i++) pq += p[i] * q[i]
    if (!(pq > 0)) break
    const alpha = rz / pq
    let rr = 0
    for (let i = 0; i < n; i++) {
      u[i] += alpha * p[i]
      r[i] -= alpha * q[i]
      rr += r[i] * r[i]
    }
    rn = Math.sqrt(rr)
    if (rn <= tol * bn) break
    let rzNew = 0
    for (let i = 0; i < n; i++) {
      z[i] = dinv[i] * r[i]
      rzNew += r[i] * z[i]
    }
    const beta = rzNew / rz
    for (let i = 0; i < n; i++) p[i] = z[i] + beta * p[i]
    rz = rzNew
  }
  info.iterations = Math.min(it, maxIter)
  info.residual = rn / bn
  for (let i = 0; i < n; i++) if (u[i] < 0) u[i] = 0
  return { u, info }
}

/**
 * heightfield.components: connected component id per vertex over the triangle edges whose both ends are NOT `cut` (the
 * free vertices of the Poisson problem — separate islands, and parts joined only through the outline, are solved and
 * normalised apart). Ids follow the smallest vertex index of each component; cut vertices get −1.
 */
export function components(n: number, T: ArrayLike<number>, cut: ArrayLike<number>): { comp: Int32Array; count: number } {
  const parent = new Int32Array(n)
  for (let i = 0; i < n; i++) parent[i] = i
  const find = (x: number) => {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]]
      x = parent[x]
    }
    return x
  }
  for (let t = 0; t < T.length; t += 3)
    for (let c = 0; c < 3; c++) {
      const a = T[t + c]
      const b = T[t + ((c + 1) % 3)]
      if (cut[a] || cut[b]) continue
      const ra = find(a)
      const rb = find(b)
      if (ra !== rb) parent[ra < rb ? rb : ra] = ra < rb ? ra : rb
    }
  const comp = new Int32Array(n).fill(-1)
  const id = new Map<number, number>()
  for (let i = 0; i < n; i++) {
    if (cut[i]) continue
    const r = find(i)
    let k = id.get(r)
    if (k === undefined) id.set(r, (k = id.size))
    comp[i] = k
  }
  return { comp, count: id.size }
}

/** heightfield._triangle_gradient: ∇u at the vertices of a P1 field — the area-weighted mean of the adjacent triangles'
 *  constant gradients (∇φ_k = left normal of the edge opposite corner k / (2·area)). Flat gx, gy pairs. */
export function triangleGradient(V: ArrayLike<number>, T: ArrayLike<number>, u: ArrayLike<number>): Float64Array {
  const n = V.length >> 1
  const g = new Float64Array(2 * n)
  const wsum = new Float64Array(n)
  for (let t = 0; t < T.length; t += 3) {
    const i = [T[t], T[t + 1], T[t + 2]]
    const a2 =
      (V[2 * i[1]] - V[2 * i[0]]) * (V[2 * i[2] + 1] - V[2 * i[0] + 1]) -
      (V[2 * i[2]] - V[2 * i[0]]) * (V[2 * i[1] + 1] - V[2 * i[0] + 1])
    const a2s = Math.abs(a2) > 1e-300 ? a2 : 1e-300
    let gx = 0
    let gy = 0
    for (let k = 0; k < 3; k++) {
      const pa = i[(k + 1) % 3]
      const pb = i[(k + 2) % 3]
      const ex = V[2 * pb] - V[2 * pa]
      const ey = V[2 * pb + 1] - V[2 * pa + 1]
      gx += u[i[k]] * -ey
      gy += u[i[k]] * ex
    }
    gx /= a2s
    gy /= a2s
    const wa = Math.abs(a2)
    for (const v of i) {
      g[2 * v] += gx * wa
      g[2 * v + 1] += gy * wa
      wsum[v] += wa
    }
  }
  for (let v = 0; v < n; v++) {
    const w = Math.max(wsum[v], 1e-300)
    g[2 * v] /= w
    g[2 * v + 1] /= w
  }
  return g
}

/** Solve the m × m system A x = y in place (Gaussian elimination, partial pivoting) -> determinant (0: singular). */
function solveDense(A: Float64Array, y: Float64Array, m: number): number {
  let det = 1
  for (let c = 0; c < m; c++) {
    let piv = c
    for (let r = c + 1; r < m; r++) if (Math.abs(A[r * m + c]) > Math.abs(A[piv * m + c])) piv = r
    const pv = A[piv * m + c]
    if (pv === 0) return 0
    if (piv !== c) {
      for (let k = 0; k < m; k++) {
        const t = A[c * m + k]
        A[c * m + k] = A[piv * m + k]
        A[piv * m + k] = t
      }
      const t = y[c]
      y[c] = y[piv]
      y[piv] = t
      det = -det
    }
    det *= pv
    for (let r = c + 1; r < m; r++) {
      const f = A[r * m + c] / pv
      if (f === 0) continue
      for (let k = c; k < m; k++) A[r * m + k] -= f * A[c * m + k]
      y[r] -= f * y[c]
    }
  }
  for (let c = m - 1; c >= 0; c--) {
    let s = y[c]
    for (let k = c + 1; k < m; k++) s -= A[c * m + k] * y[k]
    y[c] = s / A[c * m + c]
  }
  return det
}

/**
 * heightfield.vertex_gradient: ∇u at the vertices (only `where`, when given; the rest stay 0) — a least-squares QUADRATIC
 * fit of u over each vertex's one-ring (u_j − u_i ≈ g·δ + ½δᵀHδ, δ = x_j − x_i scaled by the ring's mean edge length;
 * exact for quadratics, so a disc's dome normals are exact on any mesh). Five unknowns at valence ≥ 5 when the normal
 * matrix is well conditioned (det > 1e-8 × the product of its diagonal); else (valence 3–4 too) isotropic curvature
 * H = c·I; otherwise the area-weighted mean of the adjacent triangles' constant gradients. Flat gx, gy pairs.
 */
export function vertexGradient(
  V: ArrayLike<number>,
  T: ArrayLike<number>,
  u: ArrayLike<number>,
  where?: ArrayLike<number | boolean>,
): Float64Array {
  const n = V.length >> 1
  const out = triangleGradient(V, T, u)
  const want = (i: number) => (where ? !!where[i] : true)
  for (let i = 0; i < n; i++)
    if (!want(i)) {
      out[2 * i] = 0
      out[2 * i + 1] = 0
    }
  // one-ring adjacency (unique undirected edges)
  const seen = new Set<number>()
  const deg = new Int32Array(n + 1)
  const pairs: number[] = []
  for (let t = 0; t < T.length; t += 3)
    for (let c = 0; c < 3; c++) {
      const a = T[t + c]
      const b = T[t + ((c + 1) % 3)]
      const lo = a < b ? a : b
      const hi = a < b ? b : a
      const key = lo * n + hi
      if (seen.has(key)) continue
      seen.add(key)
      pairs.push(lo, hi)
      deg[lo + 1]++
      deg[hi + 1]++
    }
  for (let i = 0; i < n; i++) deg[i + 1] += deg[i]
  const adj = new Int32Array(deg[n])
  const fill = deg.slice(0, n)
  for (let e = 0; e < pairs.length; e += 2) {
    adj[fill[pairs[e]]++] = pairs[e + 1]
    adj[fill[pairs[e + 1]]++] = pairs[e]
  }
  const A5 = new Float64Array(25)
  const y5 = new Float64Array(5)
  const A3 = new Float64Array(9)
  const y3 = new Float64Array(3)
  const row = new Float64Array(5)
  const fit = (i: number, m: number, A: Float64Array, y: Float64Array, h: number): boolean => {
    A.fill(0)
    y.fill(0)
    for (let s = deg[i]; s < deg[i + 1]; s++) {
      const j = adj[s]
      const dxn = (V[2 * j] - V[2 * i]) / h
      const dyn = (V[2 * j + 1] - V[2 * i + 1]) / h
      const du = u[j] - u[i]
      row[0] = dxn
      row[1] = dyn
      if (m === 5) {
        row[2] = 0.5 * dxn * dxn
        row[3] = dxn * dyn
        row[4] = 0.5 * dyn * dyn
      } else row[2] = 0.5 * (dxn * dxn + dyn * dyn)
      for (let r = 0; r < m; r++) {
        y[r] += row[r] * du
        for (let c = 0; c < m; c++) A[r * m + c] += row[r] * row[c]
      }
    }
    let had = 1
    for (let r = 0; r < m; r++) had *= Math.max(A[r * m + r], 1e-300)
    const det = solveDense(A, y, m)
    if (!(det > 1e-8 * had)) return false
    out[2 * i] = y[0] / h
    out[2 * i + 1] = y[1] / h
    return true
  }
  for (let i = 0; i < n; i++) {
    if (!want(i)) continue
    const cnt = deg[i + 1] - deg[i]
    if (cnt < 3) continue
    let h = 0
    for (let s = deg[i]; s < deg[i + 1]; s++) h += Math.hypot(V[2 * adj[s]] - V[2 * i], V[2 * adj[s] + 1] - V[2 * i + 1])
    h = Math.max(h / cnt, 1e-300)
    if (cnt >= 5 && fit(i, 5, A5, y5, h)) continue
    fit(i, 3, A3, y3, h)
  }
  return out
}

// ================================================================================================ build
export interface BodyArrays {
  /** Non-indexed triangle soup: positions (x, y, z per corner), per-corner normals, art-square UVs. */
  position: Float32Array
  normal: Float32Array
  uv: Float32Array
  info: {
    half: number
    wall: number
    bevel: number
    inflate: number
    D: number
    rings: number
    holes: number
    islands: number
    steiner: number
    verts: number
    faces: number
    singular: number
    fallbacks: number
    ms: number
    /** Inflated bodies: the Poisson solve (CG iterations, relative residual, free vertices, parts, ms). */
    poisson: (PoissonInfo & { components: number; ms: number }) | null
  }
}

/** Indexed form used by the checks (heightfield.build's verts / loops / starts / normals). */
export interface BodyMesh {
  verts: Float64Array // (V, 3)
  /** Triangles (top + bottom) then wall quads. */
  tris: Int32Array // (T, 3)
  triNormals: Float64Array // (T, 3 corners, 3)
  quads: Int32Array // (Q, 4)
  quadNormals: Float64Array // (Q, 4 corners, 3)
}

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

function edgesOf(T: number[]): Map<string, { a: number; b: number; n: number }> {
  const m = new Map<string, { a: number; b: number; n: number }>()
  for (let t = 0; t < T.length; t += 3)
    for (let c = 0; c < 3; c++) {
      const a = T[t + c]
      const b = T[t + ((c + 1) % 3)]
      const key = a < b ? `${a}_${b}` : `${b}_${a}`
      const e = m.get(key)
      if (e) e.n++
      else m.set(key, { a, b, n: 1 })
    }
  return m
}

/**
 * Height-field body of one piece (local units, centred on its mid-plane). Null when the outline is empty/degenerate.
 * `mesh` (optional) receives the indexed form for checks.
 */
export function buildBody(
  splines: Spline[],
  thickness: number,
  bevel: number,
  inflate = 0,
  segments = 6,
  scale = 1,
  mesh?: { out?: BodyMesh },
): BodyArrays | null {
  const t0 = now()
  const sc = Math.max(scale, 1e-9)
  const th = Math.max(thickness, MIN_THICKNESS / sc)
  let b = bevel === null || bevel === undefined ? 0 : rimBevel(th, bevel)
  const k = Math.max(0, inflate || 0)
  if (b < 1e-7 / sc) b = 0
  const tol = CHORD_TOL / sc
  const maxEdge = MAX_EDGE / sc
  const eps = MERGE_EPS / sc
  const guard = Math.min(Math.max(0.5 * b, GUARD_MIN / sc), GUARD_MAX / sc)
  const o = outline(splines, tol, maxEdge, eps, guard)
  if (!o.rings.length) return null
  const ol = new OutlineQuery(o, Math.max(NEAR / sc, 16 * tol))
  const centres = new Map<number, P2>()
  const Dr = b > 0 || k > 0 ? islandInradius(ol, 40, centres) : new Float64Array(o.rings.length)
  let steiner = b > 0 || k > 0 ? steinerPoints(ol, Dr, b, k, segments, maxEdge, TAN_MIN / sc, 1 / sc) : []
  steiner = withApices(steiner, centres, Dr, b, k, segments)
  const outlinePts: number[] = []
  for (const r of o.rings) for (const v of r) outlinePts.push(v)
  let extra = mergePoints(steiner, MERGE_Q / sc, outlinePts)
  let tri: Triangulation | null = null
  for (let pass = 0; pass <= CHORD_PASSES; pass++) {
    tri = triangulate(ol, extra)
    if (!tri.T.length) return null
    const { V, T, nOutline } = tri
    const cons = new Set<string>()
    let s = 0
    for (const r of o.rings) {
      const n = r.length >> 1
      for (let i = 0; i < n; i++) {
        const a = s + i
        const c = s + ((i + 1) % n)
        cons.add(a < c ? `${a}_${c}` : `${c}_${a}`)
      }
      s += n
    }
    const add: number[] = []
    // chords: interior edges joining two outline vertices that are not outline (constraint) edges
    for (const [key, e] of edgesOf(T)) {
      if (e.n === 2 && e.a < nOutline && e.b < nOutline && !cons.has(key))
        add.push(0.5 * (V[2 * e.a] + V[2 * e.b]), 0.5 * (V[2 * e.a + 1] + V[2 * e.b + 1]))
    }
    if (!add.length) {
      // triangles with three outline vertices and no chord (a whole small piece): their centroid
      const cen: number[] = []
      for (let t = 0; t < T.length; t += 3) {
        if (T[t] < nOutline && T[t + 1] < nOutline && T[t + 2] < nOutline) {
          cen.push(
            (V[2 * T[t]] + V[2 * T[t + 1]] + V[2 * T[t + 2]]) / 3,
            (V[2 * T[t] + 1] + V[2 * T[t + 1] + 1] + V[2 * T[t + 2] + 1]) / 3,
          )
        }
      }
      if (cen.length) {
        const dd = ol.query(cen).d
        for (let i = 0; i < dd.length; i++) if (dd[i] > 1e-6 / sc) add.push(cen[2 * i], cen[2 * i + 1])
      }
    }
    if (!add.length || pass === CHORD_PASSES) break
    extra = extra.concat(mergePoints(add, MERGE_Q / sc, outlinePts.concat(extra)))
  }
  const { V: Vall, T: Tall, nOutline } = tri!
  // ---- compact to used vertices -----------------------------------------------------------------------
  const nAll = Vall.length >> 1
  const remap = new Int32Array(nAll).fill(-1)
  let nv = 0
  for (const v of Tall) if (remap[v] < 0) remap[v] = nv++
  const V2 = new Float64Array(nv * 2)
  const onB = new Uint8Array(nv)
  for (let i = 0; i < nAll; i++) {
    const r = remap[i]
    if (r < 0) continue
    V2[2 * r] = Vall[2 * i]
    V2[2 * r + 1] = Vall[2 * i + 1]
    if (i < nOutline) onB[r] = 1
  }
  const T = Int32Array.from(Tall, (v) => remap[v])
  const nt3 = T.length / 3
  // ---- outline (boundary) edges of the kept triangulation ---------------------------------------------
  const em = edgesOf(Array.from(T))
  const beA: number[] = []
  const beB: number[] = []
  const directed = new Set<string>()
  for (let t = 0; t < T.length; t += 3)
    for (let c = 0; c < 3; c++) directed.add(`${T[t + c]}>${T[t + ((c + 1) % 3)]}`)
  for (const e of em.values()) {
    if (e.n !== 1) continue
    // directed a -> b with the interior on the left (as the CCW triangle runs it)
    if (directed.has(`${e.a}>${e.b}`)) {
      beA.push(e.a)
      beB.push(e.b)
    } else {
      beA.push(e.b)
      beB.push(e.a)
    }
  }
  for (const v of beA) onB[v] = 1
  for (const v of beB) onB[v] = 1
  const gb = new Float64Array(nv * 2)
  for (let m = 0; m < beA.length; m++) {
    const a = beA[m]
    const c = beB[m]
    let ex = V2[2 * c] - V2[2 * a]
    let ey = V2[2 * c + 1] - V2[2 * a + 1]
    const l = Math.max(Math.hypot(ex, ey), 1e-300)
    ex /= l
    ey /= l
    // inward (left) normal
    gb[2 * a] += -ey
    gb[2 * a + 1] += ex
    gb[2 * c] += -ey
    gb[2 * c + 1] += ex
  }
  // ---- distances, islands ---------------------------------------------------------------------------
  const d = new Float64Array(nv)
  const g = new Float64Array(nv * 2)
  const Dv = new Float64Array(nv).fill(1)
  const Dmax = Float64Array.from(Dr)
  const inner: number[] = []
  for (let i = 0; i < nv; i++) if (!onB[i]) inner.push(i)
  if (inner.length) {
    const P = new Float64Array(inner.length * 2)
    inner.forEach((v, m) => {
      P[2 * m] = V2[2 * v]
      P[2 * m + 1] = V2[2 * v + 1]
    })
    const q = ol.query(P, KAPPA / sc, KAPPA_REL)
    const maxBy = new Map<number, number>()
    inner.forEach((v, m) => {
      d[v] = q.d[m]
      g[2 * v] = q.g[2 * m]
      g[2 * v + 1] = q.g[2 * m + 1]
      maxBy.set(q.isl[m], Math.max(maxBy.get(q.isl[m]) ?? 0, q.d[m]))
    })
    for (const [I, mx] of maxBy) {
      const val = Math.max(Dmax[I], mx)
      o.island.forEach((J, ring) => {
        if (J === I) Dmax[ring] = val
      })
    }
    inner.forEach((v, m) => {
      Dv[v] = Math.max(Dmax[q.isl[m]], 1e-12)
    })
  }
  const loose = new Uint8Array(nv)
  for (let i = 0; i < nv; i++) {
    if (!onB[i]) continue
    const n = Math.hypot(gb[2 * i], gb[2 * i + 1])
    d[i] = 0
    if (n < 1e-9) {
      loose[i] = 1
      g[2 * i] = 0
      g[2 * i + 1] = 0
    } else {
      g[2 * i] = gb[2 * i] / n
      g[2 * i + 1] = gb[2 * i + 1] / n
    }
  }
  // ---- heights + normals ------------------------------------------------------------------------------
  let e = wallHalf(th, b)
  if (e < 1e-7 / sc) e = 0
  const z = new Float64Array(nv)
  const nt = new Float64Array(nv * 3)
  const vertical = new Uint8Array(nv)
  // the round-edge rim e + hb(d; β), β capped at the LOCAL half-width (round 8: thin parts are round tubes, small discs
  // spheres — heightfield.local_width / rim_radius), and its gradient ∂hb/∂d·g + ∂hb/∂β·∇β (g = the unit gradient of d)
  const beta = new Float64Array(nv).fill(b)
  const gbeta = new Float64Array(nv * 2)
  const cand: number[] = []
  if (b > 0) for (let i = 0; i < nv; i++) if (!onB[i] && d[i] < b) cand.push(i)
  if (cand.length) {
    const wv = localWidth(ol, Dr, WIDTH_TOL * tol, b)
    let wmin = Infinity
    for (const v of wv) if (v < wmin) wmin = v
    if (wmin < b) {
      // a wide piece (every local half-width ≥ b) keeps the plain round edge of radius b
      const Pc = new Float64Array(2 * cand.length)
      cand.forEach((v, m) => {
        Pc[2 * m] = V2[2 * v]
        Pc[2 * m + 1] = V2[2 * v + 1]
      })
      const rr = rimRadius(ol, wv, Pc, b)
      cand.forEach((v, m) => {
        beta[v] = rr.beta[m]
        gbeta[2 * v] = rr.grad[2 * m]
        gbeta[2 * v + 1] = rr.grad[2 * m + 1]
      })
    }
  }
  const e0 = wallHalf(th, b)
  const grad = new Float64Array(nv * 2)
  for (let i = 0; i < nv; i++) {
    let sd = 0
    let sb = 0
    if (b > 0) {
      z[i] = e0 + rimHeight(d[i], beta[i])
      ;[sd, sb] = rimSlopes(d[i], beta[i])
    } else z[i] = e0
    const ss = Number.isFinite(sd) ? sd : 0
    grad[2 * i] = ss * g[2 * i] + sb * gbeta[2 * i]
    grad[2 * i + 1] = ss * g[2 * i + 1] + sb * gbeta[2 * i + 1]
    // vertical tangent at the rim (round edge or dome): horizontal normals
    vertical[i] = onB[i] && !loose[i] && (b > 0 || k > 0) ? 1 : 0
  }
  let pinfo: (PoissonInfo & { components: number; ms: number }) | null = null
  if (k > 0) {
    // Poisson dome (PLAN §11 round 7): −∇²u = 4, u = 0 on the outline, per connected part normalised to its max
    const t1 = now()
    const { u, info } = poisson(V2, T, onB)
    const { comp, count } = components(nv, T, onB)
    if (count) {
      const umax = new Float64Array(count)
      const members: number[][] = Array.from({ length: count }, () => [])
      for (let i = 0; i < nv; i++) {
        const c = comp[i]
        if (c < 0) continue
        if (u[i] > umax[c]) umax[c] = u[i]
        members[c].push(Dv[i])
      }
      // D_P = the median island inradius over the part's vertices
      const Dc = members.map((m) => median(m))
      const gu = vertexGradient(V2, T, u, comp.map((c) => (c >= 0 ? 1 : 0)))
      for (let i = 0; i < nv; i++) {
        const c = comp[i]
        if (c < 0) continue
        const q = Math.min(Math.max(u[i] / Math.max(umax[c], 1e-300), 0), 1)
        const sq = Math.sqrt(Math.max(q, 1e-12))
        z[i] += k * Dc[c] * Math.sqrt(q)
        const dslope = (k * Dc[c]) / (2 * Math.max(umax[c], 1e-300) * sq)
        grad[2 * i] += dslope * gu[2 * i]
        grad[2 * i + 1] += dslope * gu[2 * i + 1]
      }
    }
    pinfo = { ...info, components: count, ms: Math.round((now() - t1) * 100) / 100 }
  }
  for (let i = 0; i < nv; i++) {
    let nx: number
    let ny: number
    let nz: number
    if (vertical[i]) {
      nx = -g[2 * i]
      ny = -g[2 * i + 1]
      nz = 0
    } else {
      nx = -grad[2 * i]
      ny = -grad[2 * i + 1]
      nz = 1
    }
    const l = Math.max(Math.hypot(nx, ny, nz), 1e-300)
    nt[3 * i] = nx / l
    nt[3 * i + 1] = ny / l
    nt[3 * i + 2] = nz / l
  }
  const P3 = new Float64Array(nv * 3)
  for (let i = 0; i < nv; i++) {
    P3[3 * i] = V2[2 * i]
    P3[3 * i + 1] = V2[2 * i + 1]
    P3[3 * i + 2] = z[i]
  }
  // face normals of the top
  const fn = new Float64Array(nt3 * 3)
  for (let t = 0; t < nt3; t++) {
    const a = T[3 * t]
    const c1 = T[3 * t + 1]
    const c2 = T[3 * t + 2]
    const ux = P3[3 * c1] - P3[3 * a]
    const uy = P3[3 * c1 + 1] - P3[3 * a + 1]
    const uz = P3[3 * c1 + 2] - P3[3 * a + 2]
    const vx = P3[3 * c2] - P3[3 * a]
    const vy = P3[3 * c2 + 1] - P3[3 * a + 1]
    const vz = P3[3 * c2 + 2] - P3[3 * a + 2]
    let x = uy * vz - uz * vy
    let y = uz * vx - ux * vz
    let w = ux * vy - uy * vx
    const l = Math.max(Math.hypot(x, y, w), 1e-300)
    x /= l
    y /= l
    w /= l
    fn[3 * t] = x
    fn[3 * t + 1] = y
    fn[3 * t + 2] = w
  }
  // safe normals: blend free (non-vertical) vertex normals toward +Z until they face all their top faces
  {
    const todo = new Uint8Array(nv)
    for (let i = 0; i < nv; i++) todo[i] = vertical[i] ? 0 : 1
    const out = Float64Array.from(nt)
    const mind = new Float64Array(nv)
    const cand = new Float64Array(nv * 3)
    for (const lam of [0, 0.25, 0.5, 0.75, 0.9, 1]) {
      for (let i = 0; i < nv; i++) {
        const x = (1 - lam) * nt[3 * i]
        const y = (1 - lam) * nt[3 * i + 1]
        const w = (1 - lam) * nt[3 * i + 2] + lam
        const l = Math.max(Math.hypot(x, y, w), 1e-300)
        cand[3 * i] = x / l
        cand[3 * i + 1] = y / l
        cand[3 * i + 2] = w / l
      }
      mind.fill(Infinity)
      for (let t = 0; t < nt3; t++)
        for (let c = 0; c < 3; c++) {
          const v = T[3 * t + c]
          const dt = cand[3 * v] * fn[3 * t] + cand[3 * v + 1] * fn[3 * t + 1] + cand[3 * v + 2] * fn[3 * t + 2]
          if (dt < mind[v]) mind[v] = dt
        }
      let left = false
      for (let i = 0; i < nv; i++) {
        if (!todo[i]) continue
        if (mind[i] >= NORMAL_MIN_DOT) {
          out[3 * i] = cand[3 * i]
          out[3 * i + 1] = cand[3 * i + 1]
          out[3 * i + 2] = cand[3 * i + 2]
          todo[i] = 0
        } else left = true
      }
      if (!left) break
    }
    for (let i = 0; i < nv; i++)
      if (todo[i]) {
        out[3 * i] = 0
        out[3 * i + 1] = 0
        out[3 * i + 2] = 1
      }
    nt.set(out)
  }
  // ---- per-corner normals of the top; singular outline vertices -------------------------------------
  const CT = new Float64Array(nt3 * 9)
  const mind = new Float64Array(nv).fill(Infinity)
  for (let t = 0; t < nt3; t++)
    for (let c = 0; c < 3; c++) {
      const v = T[3 * t + c]
      CT[9 * t + 3 * c] = nt[3 * v]
      CT[9 * t + 3 * c + 1] = nt[3 * v + 1]
      CT[9 * t + 3 * c + 2] = nt[3 * v + 2]
      const dt = nt[3 * v] * fn[3 * t] + nt[3 * v + 1] * fn[3 * t + 1] + nt[3 * v + 2] * fn[3 * t + 2]
      if (dt < mind[v]) mind[v] = dt
    }
  // wall normals (outward) per boundary edge
  const wn = new Float64Array(beA.length * 2)
  for (let m = 0; m < beA.length; m++) {
    const ex = V2[2 * beB[m]] - V2[2 * beA[m]]
    const ey = V2[2 * beB[m] + 1] - V2[2 * beA[m] + 1]
    const l = Math.max(Math.hypot(ex, ey), 1e-300)
    wn[2 * m] = ey / l
    wn[2 * m + 1] = -ex / l
  }
  const hz = new Float64Array(nv * 2)
  for (let i = 0; i < nv; i++) {
    const l = Math.max(Math.hypot(g[2 * i], g[2 * i + 1]), 1e-300)
    hz[2 * i] = -g[2 * i] / l
    hz[2 * i + 1] = -g[2 * i + 1] / l
  }
  if (wallHalf(th, b) >= 1e-7 / sc && beA.length) {
    for (let m = 0; m < beA.length; m++)
      for (const v of [beA[m], beB[m]]) {
        const dt = hz[2 * v] * wn[2 * m] + hz[2 * v + 1] * wn[2 * m + 1]
        if (dt < mind[v]) mind[v] = dt
      }
  }
  const sing = new Uint8Array(nv)
  let nSing = 0
  for (let i = 0; i < nv; i++)
    if (onB[i] && mind[i] < NORMAL_MIN_DOT) {
      sing[i] = 1
      nSing++
    }
  if (nSing) {
    for (let t = 0; t < nt3; t++)
      for (let c = 0; c < 3; c++) {
        if (!sing[T[3 * t + c]]) continue
        const v1 = T[3 * t + ((c + 1) % 3)]
        const v2 = T[3 * t + ((c + 2) % 3)]
        let x = nt[3 * v1] + nt[3 * v2]
        let y = nt[3 * v1 + 1] + nt[3 * v2 + 1]
        let w = nt[3 * v1 + 2] + nt[3 * v2 + 2]
        const l = Math.max(Math.hypot(x, y, w), 1e-300)
        x /= l
        y /= l
        w /= l
        if (x * fn[3 * t] + y * fn[3 * t + 1] + w * fn[3 * t + 2] < NORMAL_MIN_DOT) {
          x = fn[3 * t]
          y = fn[3 * t + 1]
          w = fn[3 * t + 2]
        }
        CT[9 * t + 3 * c] = x
        CT[9 * t + 3 * c + 1] = y
        CT[9 * t + 3 * c + 2] = w
      }
  }
  // ---- assemble ---------------------------------------------------------------------------------------
  const walls = e > 0
  let nIn = 0
  for (let i = 0; i < nv; i++) if (!onB[i]) nIn++
  const nb = new Int32Array(nv)
  let nVerts: number
  if (walls) {
    for (let i = 0; i < nv; i++) nb[i] = i + nv
    nVerts = 2 * nv
  } else {
    let c = nv
    for (let i = 0; i < nv; i++) nb[i] = onB[i] ? i : c++
    nVerts = nv + nIn
  }
  const verts = new Float64Array(nVerts * 3)
  verts.set(P3)
  for (let i = 0; i < nv; i++) {
    const j = nb[i]
    if (j < nv) continue
    verts[3 * j] = V2[2 * i]
    verts[3 * j + 1] = V2[2 * i + 1]
    verts[3 * j + 2] = -z[i]
  }
  const nQ = walls ? beA.length : 0
  const tris = new Int32Array(2 * nt3 * 3)
  const triN = new Float64Array(2 * nt3 * 9)
  for (let t = 0; t < nt3; t++) {
    tris.set([T[3 * t], T[3 * t + 1], T[3 * t + 2]], 3 * t)
    triN.set(CT.subarray(9 * t, 9 * t + 9), 9 * t)
    const u = nt3 + t
    tris.set([nb[T[3 * t]], nb[T[3 * t + 2]], nb[T[3 * t + 1]]], 3 * u)
    for (const [c, src] of [
      [0, 0],
      [1, 2],
      [2, 1],
    ] as const) {
      triN[9 * u + 3 * c] = CT[9 * t + 3 * src]
      triN[9 * u + 3 * c + 1] = CT[9 * t + 3 * src + 1]
      triN[9 * u + 3 * c + 2] = -CT[9 * t + 3 * src + 2]
    }
  }
  const quads = new Int32Array(nQ * 4)
  const quadN = new Float64Array(nQ * 12)
  if (walls) {
    let flatRim = true
    for (let i = 0; i < nv; i++) if (onB[i] && vertical[i]) flatRim = false
    const crease = Uint8Array.from(sing)
    if (flatRim) {
      // flat rim (no bevel, no inflate): a corner turning more than CORNER_DEG is a vertical crease too
      const mw = new Float64Array(nv).fill(Infinity)
      for (let m = 0; m < nQ; m++)
        for (const v of [beA[m], beB[m]]) {
          const dt = hz[2 * v] * wn[2 * m] + hz[2 * v + 1] * wn[2 * m + 1]
          if (dt < mw[v]) mw[v] = dt
        }
      const lim = Math.cos(((CORNER_DEG / 2) * Math.PI) / 180)
      for (let i = 0; i < nv; i++) if (onB[i] && mw[i] < lim) crease[i] = 1
    }
    for (let m = 0; m < nQ; m++) {
      const a = beA[m]
      const c = beB[m]
      quads.set([c, a, nb[a], nb[c]], 4 * m)
      const corners = [c, a, a, c]
      corners.forEach((v, j) => {
        if (crease[v]) quadN.set([wn[2 * m], wn[2 * m + 1], 0], 12 * m + 3 * j)
        else quadN.set([hz[2 * v], hz[2 * v + 1], 0], 12 * m + 3 * j)
      })
    }
  }
  if (mesh) mesh.out = { verts, tris, triNormals: triN, quads, quadNormals: quadN }
  // ---- triangle soup for three.js -------------------------------------------------------------------
  const nTri = 2 * nt3 + 2 * nQ
  const position = new Float32Array(nTri * 9)
  const normal = new Float32Array(nTri * 9)
  const uv = new Float32Array(nTri * 6)
  let w = 0
  const put = (v: number, nx: number, ny: number, nz: number) => {
    const x = verts[3 * v]
    const y = verts[3 * v + 1]
    position[3 * w] = x
    position[3 * w + 1] = y
    position[3 * w + 2] = verts[3 * v + 2]
    normal[3 * w] = nx
    normal[3 * w + 1] = ny
    normal[3 * w + 2] = nz
    uv[2 * w] = (x + 1) / 2
    uv[2 * w + 1] = (y + 1) / 2
    w++
  }
  for (let t = 0; t < 2 * nt3; t++)
    for (let c = 0; c < 3; c++) put(tris[3 * t + c], triN[9 * t + 3 * c], triN[9 * t + 3 * c + 1], triN[9 * t + 3 * c + 2])
  for (let m = 0; m < nQ; m++) {
    for (const j of [0, 1, 2, 0, 2, 3])
      put(quads[4 * m + j], quadN[12 * m + 3 * j], quadN[12 * m + 3 * j + 1], quadN[12 * m + 3 * j + 2])
  }
  let half = 0
  for (let i = 0; i < nv; i++) half = Math.max(half, z[i])
  let Dm = 0
  for (const v of Dmax) Dm = Math.max(Dm, v)
  return {
    position,
    normal,
    uv,
    info: {
      half,
      wall: e,
      bevel: b,
      inflate: k,
      D: Dm,
      rings: o.rings.length,
      holes: o.hole.filter(Boolean).length,
      islands: new Set(o.island).size,
      steiner: steiner.length >> 1,
      verts: nVerts,
      faces: 2 * nt3 + nQ,
      singular: nSing,
      fallbacks: tri!.fallbacks,
      ms: Math.round((now() - t0) * 100) / 100,
      poisson: pinfo,
    },
  }
}

/** Largest island inradius of a piece (local units) — framing / lift estimates (heightfield.inradius). */
export function inradius(splines: Spline[], scale = 1): number {
  const sc = Math.max(scale, 1e-9)
  const o = outline(splines, CHORD_TOL / sc, MAX_EDGE / sc, MERGE_EPS / sc)
  if (!o.rings.length) return 0
  let D = 0
  for (const v of islandInradius(new OutlineQuery(o, 0))) D = Math.max(D, v)
  return D
}

// ================================================================================================ pieces of one layer
/** heightfield.piece_rings: the sample rings (local units) of a piece's outline — outline() at the body tolerances. */
export function pieceRings(splines: Spline[], scale = 1): Ring[] {
  const sc = Math.max(scale, 1e-9)
  return outline(splines, CHORD_TOL / sc, MAX_EDGE / sc, MERGE_EPS / sc).rings
}

/** Even-odd inside test of (x, y) against a piece's rings. */
function insideRings(x: number, y: number, rings: Ring[]): boolean {
  let m = false
  for (const r of rings) if (pointInRing(x, y, r)) m = !m
  return m
}

/** heightfield._interior_probes: points `depth` inside a piece along each outline vertex's inward bisector (rings
 *  oriented material on the left), kept only where they really lie inside, at least depth/2 from the outline. */
export function interiorProbes(rings: Ring[], depth: number): number[] {
  const P: number[] = []
  for (const r of rings) {
    const n = r.length >> 1
    if (n < 3) continue
    for (let i = 0; i < n; i++) {
      const p = (i - 1 + n) % n
      const q = (i + 1) % n
      let tix = r[2 * i] - r[2 * p]
      let tiy = r[2 * i + 1] - r[2 * p + 1]
      let l = Math.max(Math.hypot(tix, tiy), 1e-300)
      tix /= l
      tiy /= l
      let tox = r[2 * q] - r[2 * i]
      let toy = r[2 * q + 1] - r[2 * i + 1]
      l = Math.max(Math.hypot(tox, toy), 1e-300)
      tox /= l
      toy /= l
      let nx = -tiy - toy
      let ny = tix + tox
      l = Math.max(Math.hypot(nx, ny), 1e-300)
      nx /= l
      ny /= l
      P.push(r[2 * i] + nx * depth, r[2 * i + 1] + ny * depth)
    }
  }
  if (!P.length) return P
  const d = nearest(P, new SegmentSet(rings)).d
  const out: number[] = []
  for (let i = 0; i < d.length; i++) if (d[i] >= 0.5 * depth && insideRings(P[2 * i], P[2 * i + 1], rings)) out.push(P[2 * i], P[2 * i + 1])
  return out
}

/**
 * heightfield.rings_relation — how two pieces (lists of rings) meet: 0 apart, 1 TOUCH (outlines within `tol` of each
 * other — a shared edge — but the interiors do not overlap), 2 OVERLAP (some vertex of one lies inside the other,
 * farther than `tol` from its outline: a translucent piece over another — or the outlines (nearly) COINCIDE: caught by
 * interior probes 4·tol inside each piece).
 */
export function ringsRelation(ra: Ring[], rb: Ring[], tol: number): 0 | 1 | 2 {
  if (!ra.length || !rb.length) return 0
  const box = (rs: Ring[]) => {
    let x0 = Infinity
    let y0 = Infinity
    let x1 = -Infinity
    let y1 = -Infinity
    for (const r of rs) {
      const b = ringBox(r)
      x0 = Math.min(x0, b[0])
      y0 = Math.min(y0, b[1])
      x1 = Math.max(x1, b[2])
      y1 = Math.max(y1, b[3])
    }
    return [x0, y0, x1, y1]
  }
  const [ax0, ay0, ax1, ay1] = box(ra)
  const [bx0, by0, bx1, by1] = box(rb)
  if (ax0 > bx1 + tol || bx0 > ax1 + tol || ay0 > by1 + tol || by0 > ay1 + tol) return 0
  const flat = (rs: Ring[]) => {
    const out: number[] = []
    for (const r of rs) for (const v of r) out.push(v)
    return out
  }
  const A = flat(ra)
  const B = flat(rb)
  const sa = new SegmentSet(ra)
  const sb = new SegmentSet(rb)
  const da = nearest(A, sb).d // A's vertices to B's outline
  const db = nearest(B, sa).d
  for (let i = 0; i < da.length; i++) if (da[i] > tol && insideRings(A[2 * i], A[2 * i + 1], rb)) return 2
  for (let i = 0; i < db.length; i++) if (db[i] > tol && insideRings(B[2 * i], B[2 * i + 1], ra)) return 2
  if (!da.some((v) => v <= tol) && !db.some((v) => v <= tol)) return 0
  // the outlines meet: a shared edge (TOUCH) — or (nearly) the same outline, interiors on the same side (OVERLAP)
  for (const [r1, r2, s2] of [
    [ra, rb, sb],
    [rb, ra, sa],
  ] as const) {
    const Q = interiorProbes(r1, 4 * tol)
    if (!Q.length) continue
    const dq = nearest(Q, s2).d
    for (let i = 0; i < dq.length; i++) if (dq[i] > tol && insideRings(Q[2 * i], Q[2 * i + 1], r2)) return 2
  }
  return 1
}

/**
 * heightfield.inset_rings: piece A's rings pulled back from piece B (they touch along a shared edge) — every vertex of A
 * inside B or closer than `gap` to B's outline moves to B's outline + `gap` along B's outward normal there (into A).
 */
export function insetRings(ra: Ring[], rb: Ring[], gap: number): Ring[] {
  if (!ra.length || !rb.length) return ra
  const sb = new SegmentSet(rb)
  return ra.map((r) => {
    const { d, seg, g } = nearest(r, sb)
    let q: Float64Array | null = null
    for (let i = 0; i < d.length; i++) {
      if (!(d[i] < gap) && !insideRings(r[2 * i], r[2 * i + 1], rb)) continue
      q ??= Float64Array.from(r)
      const fx = r[2 * i] - g[2 * i] * d[i]
      const fy = r[2 * i + 1] - g[2 * i + 1] * d[i]
      const k = seg[i]
      const ex = sb.bx[k] - sb.ax[k]
      const ey = sb.by[k] - sb.ay[k]
      const l = Math.max(Math.hypot(ex, ey), 1e-300)
      // B's outward normal (material on the left → outward = right normal)
      q[2 * i] = fx + (ey / l) * gap
      q[2 * i + 1] = fy + (-ex / l) * gap
    }
    return q ?? r
  })
}

/** heightfield.rings_to_splines: polyline rings → closed straight-segment splines (the builder's input). */
export function ringsToSplines(rings: Ring[]): Spline[] {
  return rings.map((r) => {
    const points = []
    for (let i = 0; i < r.length; i += 2) {
      const co: [number, number] = [r[i], r[i + 1]]
      points.push({ co, hl: co, hr: co })
    }
    return { closed: true, hole: false, parent: -1, depth: 0, points }
  })
}

/**
 * heightfield.stack_shifts — real-height stacking INSIDE one layer (paint order): every body is centred at base[j] +
 * shift; a body that touches / overlaps an earlier one (`pairs` = [i, j], i < j) is lifted until its lowest point clears
 * that body's top by `gap`: shift_j = max(0, max_i (base_i + shift_i + h_i + gap + h_j) − base_j).
 */
export function stackShifts(n: number, pairs: readonly (readonly [number, number])[], halves: ArrayLike<number>, gap: number, base?: ArrayLike<number>): Float64Array {
  const s = new Float64Array(n)
  const below = new Map<number, number[]>()
  for (const [i, j] of pairs) {
    let l = below.get(j)
    if (!l) below.set(j, (l = []))
    l.push(i)
  }
  const b0 = (k: number) => (base ? base[k] : 0)
  for (let j = 0; j < n; j++)
    for (const i of below.get(j) ?? []) s[j] = Math.max(s[j], b0(i) + s[i] + halves[i] + gap + halves[j] - b0(j))
  return s
}

// ================================================================================================ checks
/**
 * Topology report of a body (heightfield.check_arrays + the BVH self-intersection test of check_mesh): non-manifold /
 * mis-wound edges, signed volume, corner normals facing away from their faces, intersecting non-adjacent triangles.
 */
export function checkBody(m: BodyMesh, intersections = true) {
  const V = m.verts
  const faces: number[][] = []
  const fnorm: number[][] = []
  for (let t = 0; t < m.tris.length / 3; t++) {
    faces.push([m.tris[3 * t], m.tris[3 * t + 1], m.tris[3 * t + 2]])
    fnorm.push(Array.from(m.triNormals.subarray(9 * t, 9 * t + 9)))
  }
  for (let q = 0; q < m.quads.length / 4; q++) {
    faces.push(Array.from(m.quads.subarray(4 * q, 4 * q + 4)))
    fnorm.push(Array.from(m.quadNormals.subarray(12 * q, 12 * q + 12)))
  }
  const edges = new Map<string, { n: number; fwd: number }>()
  let vol = 0
  let inverted = 0
  let minDot = 1
  const tri: number[][] = [] // triangle soup for the intersection test
  faces.forEach((f, fi) => {
    let nx = 0
    let ny = 0
    let nz = 0
    for (let j = 1; j < f.length - 1; j++) {
      const p0 = f[0]
      const p1 = f[j]
      const p2 = f[j + 1]
      const ax = V[3 * p1] - V[3 * p0]
      const ay = V[3 * p1 + 1] - V[3 * p0 + 1]
      const az = V[3 * p1 + 2] - V[3 * p0 + 2]
      const bx = V[3 * p2] - V[3 * p0]
      const by = V[3 * p2 + 1] - V[3 * p0 + 1]
      const bz = V[3 * p2 + 2] - V[3 * p0 + 2]
      const cx = ay * bz - az * by
      const cy = az * bx - ax * bz
      const cz = ax * by - ay * bx
      vol += (V[3 * p0] * cx + V[3 * p0 + 1] * cy + V[3 * p0 + 2] * cz) / 6
      nx += cx
      ny += cy
      nz += cz
      tri.push([p0, p1, p2])
    }
    const nl = Math.hypot(nx, ny, nz)
    for (let c = 0; c < f.length; c++) {
      const a = f[c]
      const b = f[(c + 1) % f.length]
      const key = a < b ? `${a}_${b}` : `${b}_${a}`
      const e = edges.get(key) ?? { n: 0, fwd: 0 }
      e.n++
      if (a < b) e.fwd++
      edges.set(key, e)
      if (nl > 1e-14) {
        const dt = (fnorm[fi][3 * c] * nx + fnorm[fi][3 * c + 1] * ny + fnorm[fi][3 * c + 2] * nz) / nl
        if (dt < -1e-3) inverted++
        minDot = Math.min(minDot, dt)
      }
    }
  })
  let nonManifold = 0
  let misoriented = 0
  for (const e of edges.values()) {
    if (e.n !== 2) nonManifold++
    else if (e.fwd !== 1) misoriented++
  }
  return {
    nonManifold,
    misoriented,
    volume: vol,
    invertedNormals: inverted,
    minNormalDot: minDot,
    selfIntersections: intersections ? countIntersections(V, tri) : -1,
  }
}

/** Pairs of non-adjacent triangles that intersect (uniform grid broad phase + Möller's tri-tri test). */
function countIntersections(V: Float64Array, tris: number[][]): number {
  const n = tris.length
  const box = new Float64Array(n * 6)
  let ext = 1e-9
  let gx0 = Infinity
  let gy0 = Infinity
  let gz0 = Infinity
  let gx1 = -Infinity
  let gy1 = -Infinity
  let gz1 = -Infinity
  tris.forEach((t, i) => {
    let x0 = Infinity
    let y0 = Infinity
    let z0 = Infinity
    let x1 = -Infinity
    let y1 = -Infinity
    let z1 = -Infinity
    for (const v of t) {
      x0 = Math.min(x0, V[3 * v])
      x1 = Math.max(x1, V[3 * v])
      y0 = Math.min(y0, V[3 * v + 1])
      y1 = Math.max(y1, V[3 * v + 1])
      z0 = Math.min(z0, V[3 * v + 2])
      z1 = Math.max(z1, V[3 * v + 2])
    }
    box.set([x0, y0, z0, x1, y1, z1], 6 * i)
    gx0 = Math.min(gx0, x0)
    gy0 = Math.min(gy0, y0)
    gz0 = Math.min(gz0, z0)
    gx1 = Math.max(gx1, x1)
    gy1 = Math.max(gy1, y1)
    gz1 = Math.max(gz1, z1)
  })
  ext = Math.max(gx1 - gx0, gy1 - gy0, 1e-9)
  const cell = ext / Math.max(8, Math.min(128, Math.round(Math.sqrt(n) / 2)))
  const grid = new Map<string, number[]>()
  for (let i = 0; i < n; i++) {
    const i0 = Math.floor((box[6 * i] - gx0) / cell)
    const i1 = Math.floor((box[6 * i + 3] - gx0) / cell)
    const j0 = Math.floor((box[6 * i + 1] - gy0) / cell)
    const j1 = Math.floor((box[6 * i + 4] - gy0) / cell)
    for (let a = i0; a <= i1; a++)
      for (let b = j0; b <= j1; b++) {
        const k = `${a},${b}`
        let l = grid.get(k)
        if (!l) grid.set(k, (l = []))
        l.push(i)
      }
  }
  const seen = new Set<number>()
  let count = 0
  for (const l of grid.values())
    for (let p = 0; p < l.length; p++)
      for (let q = p + 1; q < l.length; q++) {
        const i = Math.min(l[p], l[q])
        const j = Math.max(l[p], l[q])
        const key = i * n + j
        if (seen.has(key)) continue
        seen.add(key)
        const A = tris[i]
        const B = tris[j]
        if (A.some((v) => B.includes(v))) continue
        if (
          box[6 * i] > box[6 * j + 3] ||
          box[6 * j] > box[6 * i + 3] ||
          box[6 * i + 1] > box[6 * j + 4] ||
          box[6 * j + 1] > box[6 * i + 4] ||
          box[6 * i + 2] > box[6 * j + 5] ||
          box[6 * j + 2] > box[6 * i + 5]
        )
          continue
        if (triTri(V, A, B)) count++
      }
  return count
}

type V3 = [number, number, number]
const sub3 = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
const cross3 = (a: V3, b: V3): V3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
const dot3 = (a: V3, b: V3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

/** Proper intersection of two triangles (strict: touching within 1e-12 does not count). */
function triTri(V: Float64Array, A: number[], B: number[]): boolean {
  const p = (v: number): V3 => [V[3 * v], V[3 * v + 1], V[3 * v + 2]]
  const a = A.map(p)
  const b = B.map(p)
  const eps = 1e-12
  const n1 = cross3(sub3(a[1], a[0]), sub3(a[2], a[0]))
  const db = b.map((x) => dot3(n1, sub3(x, a[0])))
  if ((db[0] > eps && db[1] > eps && db[2] > eps) || (db[0] < -eps && db[1] < -eps && db[2] < -eps)) return false
  const n2 = cross3(sub3(b[1], b[0]), sub3(b[2], b[0]))
  const da = a.map((x) => dot3(n2, sub3(x, b[0])))
  if ((da[0] > eps && da[1] > eps && da[2] > eps) || (da[0] < -eps && da[1] < -eps && da[2] < -eps)) return false
  const dir = cross3(n1, n2)
  if (dot3(dir, dir) < 1e-24) return false // coplanar: adjacent caps never overlap by construction
  const interval = (t: V3[], dd: number[]): [number, number] | null => {
    const proj = t.map((x) => dot3(dir, x))
    const pts: number[] = []
    for (let i = 0; i < 3; i++) {
      const j = (i + 1) % 3
      if ((dd[i] > 0 && dd[j] < 0) || (dd[i] < 0 && dd[j] > 0)) pts.push(proj[i] + ((proj[j] - proj[i]) * dd[i]) / (dd[i] - dd[j]))
      else if (Math.abs(dd[i]) <= eps) pts.push(proj[i])
    }
    if (pts.length < 2) return null
    return [Math.min(...pts), Math.max(...pts)]
  }
  const i1 = interval(a, da)
  const i2 = interval(b, db)
  if (!i1 || !i2) return false
  const lo = Math.max(i1[0], i2[0])
  const hi = Math.min(i1[1], i2[1])
  return hi - lo > 1e-9 * Math.max(1, Math.abs(hi))
}
