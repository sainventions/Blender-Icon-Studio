// Layer footprints and the XY overlap test of the overlap-aware stack (PLAN §11 round 8; server bis.stacking
// footprints / overlap_lists / interpenetrations). Pure (no three.js, no store): the node tests import it directly.
//
// A layer's FOOTPRINT is its silhouette + its regions (occlusion-cut pieces) + the bbox of every raster image that has no
// region (a flat card), from the geometry bundle, in ART units — the server's shape_from_geometry. Placed on the canvas
// like the worker places the bodies: p · art.scale · layer.scale + art.xy · layer.scale + layer.xy. Two layers OVERLAP
// when their footprints come closer than the clearance (the presets' stackGap): shapely.dwithin. The server simplifies
// its footprints by 0.002 first; this test is exact on the flattened splines (≪ the 0.03 clearance either way).
import type { ArtTransform, Layer, LayerGeometry, Spline } from '../../types'

/** Bezier flattening tolerance of footprint splines (art units; server stacking.FLATTEN_TOL). */
export const FLATTEN_TOL = 5e-4

/** One even-odd polygon (rings = flat x,y pairs). A footprint is the union of its parts. */
interface Part {
  rings: Float64Array[]
}
/** A footprint in art units (cached per LayerGeometry hash). Empty parts = overlaps nothing. */
export interface Footprint {
  parts: Part[]
}
/** A footprint placed on the canvas: rings + segment soup + bbox. */
export interface Placed {
  parts: Part[]
  ax: Float64Array
  ay: Float64Array
  bx: Float64Array
  by: Float64Array
  box: [number, number, number, number]
}

const num = (v: unknown, d = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : d)

/** A closed cubic spline flattened like the server's stacking.spline_ring: per segment ⌈√(0.75·M / tol)⌉ ∈ [1, 64]
 *  uniform-t samples, M = max |P0 − 2P1 + P2|, |P1 − 2P2 + P3| (Wang's bound). */
export function splineRing(s: Spline, tol = FLATTEN_TOL): Float64Array | null {
  const pts = s.points ?? []
  const n = pts.length
  if (n < 2) return null
  const out: number[] = []
  for (let i = 0; i < n; i++) {
    const a = pts[i]
    const c = pts[(i + 1) % n]
    const P0 = a.co
    const P1 = a.hr ?? a.co
    const P2 = c.hl ?? c.co
    const P3 = c.co
    const M = Math.max(
      Math.hypot(P0[0] - 2 * P1[0] + P2[0], P0[1] - 2 * P1[1] + P2[1]),
      Math.hypot(P1[0] - 2 * P2[0] + P3[0], P1[1] - 2 * P2[1] + P3[1]),
    )
    const m = Math.min(64, Math.max(1, Math.ceil(Math.sqrt((0.75 * M) / Math.max(tol, 1e-9)))))
    for (let k = 0; k < m; k++) {
      const t = k / m
      const u = 1 - t
      const w0 = u * u * u
      const w1 = 3 * u * u * t
      const w2 = 3 * u * t * t
      const w3 = t * t * t
      out.push(w0 * P0[0] + w1 * P1[0] + w2 * P2[0] + w3 * P3[0], w0 * P0[1] + w1 * P1[1] + w2 * P2[1] + w3 * P3[1])
    }
  }
  return Float64Array.from(out)
}

function ringArea(r: Float64Array): number {
  let a = 0
  const n = r.length >> 1
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n
    a += r[2 * i] * r[2 * j + 1] - r[2 * j] * r[2 * i + 1]
  }
  return a / 2
}

function splinesPart(splines: Spline[] | null | undefined): Part | null {
  const rings: Float64Array[] = []
  for (const s of splines ?? []) {
    const r = splineRing(s)
    if (r && r.length >= 6 && Math.abs(ringArea(r)) > 0) rings.push(r)
  }
  return rings.length ? { rings } : null
}

const fpCache = new Map<string, Footprint>()

/** The footprint of a layer's geometry (server stacking.shape_from_geometry): silhouette + regions + the bbox of every
 *  raster image without a region. Cached by the layer geometry hash. */
export function layerFootprint(lg: LayerGeometry): Footprint {
  const key = `${lg.layerId}:${lg.hash}`
  const hit = lg.hash ? fpCache.get(key) : undefined
  if (hit) return hit
  const parts: Part[] = []
  const sil = splinesPart(lg.silhouette)
  if (sil) parts.push(sil)
  const covered = new Set<string>()
  for (const r of lg.regions ?? []) {
    covered.add(r.elementId)
    const p = splinesPart(r.splines)
    if (p) parts.push(p)
  }
  for (const im of lg.images ?? []) {
    const bb = im?.bbox
    if (!im || covered.has(im.elementId) || !bb || bb.length !== 4) continue
    const [x0, y0, x1, y1] = bb.map((v) => num(v))
    if (x1 > x0 && y1 > y0) parts.push({ rings: [Float64Array.from([x0, y0, x1, y0, x1, y1, x0, y1])] })
  }
  const fp = { parts }
  if (lg.hash) {
    if (fpCache.size > 256) fpCache.clear()
    fpCache.set(key, fp)
  }
  return fp
}

/** The canvas placement of a layer's art: p · a + (ox, oy) — the worker's (p · art.scale + art.xy) · layer.scale + layer.xy. */
export function placement(art: ArtTransform, l: Pick<Layer, 'transform'>): [number, number, number] {
  const sl = Math.max(0, num(l.transform?.scale, 1))
  const a = Math.max(0, num(art?.scale, 1)) * sl
  return [a, num(art?.x) * sl + num(l.transform?.x), num(art?.y) * sl + num(l.transform?.y)]
}

/** A footprint placed on the canvas (scale a, offset ox / oy). */
export function place(fp: Footprint, a: number, ox: number, oy: number): Placed {
  const parts: Part[] = fp.parts.map((p) => ({ rings: p.rings.map((r) => r.map((v, i) => (i & 1 ? v * a + oy : v * a + ox))) }))
  let M = 0
  for (const p of parts) for (const r of p.rings) M += r.length >> 1
  const ax = new Float64Array(M)
  const ay = new Float64Array(M)
  const bx = new Float64Array(M)
  const by = new Float64Array(M)
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  let g = 0
  for (const p of parts)
    for (const r of p.rings) {
      const n = r.length >> 1
      for (let i = 0; i < n; i++, g++) {
        const j = (i + 1) % n
        ax[g] = r[2 * i]
        ay[g] = r[2 * i + 1]
        bx[g] = r[2 * j]
        by[g] = r[2 * j + 1]
        x0 = Math.min(x0, ax[g])
        y0 = Math.min(y0, ay[g])
        x1 = Math.max(x1, ax[g])
        y1 = Math.max(y1, ay[g])
      }
    }
  return { parts, ax, ay, bx, by, box: [x0, y0, x1, y1] }
}

function pointInRings(x: number, y: number, rings: Float64Array[]): boolean {
  let inside = false
  for (const r of rings) {
    const n = r.length >> 1
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const yi = r[2 * i + 1]
      const yj = r[2 * j + 1]
      if (yi > y !== yj > y && x < ((r[2 * j] - r[2 * i]) * (y - yi)) / (yj - yi) + r[2 * i]) inside = !inside
    }
  }
  return inside
}

/** Is (x, y) inside the footprint (the union of its even-odd parts)? */
export function insidePlaced(P: Placed, x: number, y: number): boolean {
  return P.parts.some((p) => pointInRings(x, y, p.rings))
}

function segPointDist2(ax: number, ay: number, bx: number, by: number, px: number, py: number): number {
  const ex = bx - ax
  const ey = by - ay
  const L2 = ex * ex + ey * ey
  let t = L2 > 1e-30 ? ((px - ax) * ex + (py - ay) * ey) / L2 : 0
  t = t < 0 ? 0 : t > 1 ? 1 : t
  const dx = px - ax - t * ex
  const dy = py - ay - t * ey
  return dx * dx + dy * dy
}

const orient = (ax: number, ay: number, bx: number, by: number, cx: number, cy: number) => (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

/** Squared distance between segments a0a1 and b0b1 (0 when they cross). */
function segSegDist2(a0x: number, a0y: number, a1x: number, a1y: number, b0x: number, b0y: number, b1x: number, b1y: number): number {
  const d1 = orient(a0x, a0y, a1x, a1y, b0x, b0y)
  const d2 = orient(a0x, a0y, a1x, a1y, b1x, b1y)
  const d3 = orient(b0x, b0y, b1x, b1y, a0x, a0y)
  const d4 = orient(b0x, b0y, b1x, b1y, a1x, a1y)
  if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) return 0
  return Math.min(
    segPointDist2(a0x, a0y, a1x, a1y, b0x, b0y),
    segPointDist2(a0x, a0y, a1x, a1y, b1x, b1y),
    segPointDist2(b0x, b0y, b1x, b1y, a0x, a0y),
    segPointDist2(b0x, b0y, b1x, b1y, a1x, a1y),
  )
}

/** shapely.dwithin(A, B, c) of two placed footprints (c = 0: they intersect or touch): some outline of one comes
 *  within c of the other's, or one lies inside the other (a ring vertex inside it — outlines farther than c apart
 *  cannot cross, so any vertex of a ring decides for the whole ring). Empty footprints overlap nothing. */
export function dwithin(A: Placed, B: Placed, c: number): boolean {
  if (!A.ax.length || !B.ax.length) return false
  const [ax0, ay0, ax1, ay1] = A.box
  const [bx0, by0, bx1, by1] = B.box
  const gx = Math.max(0, bx0 - ax1, ax0 - bx1)
  const gy = Math.max(0, by0 - ay1, ay0 - by1)
  if (gx * gx + gy * gy > c * c) return false
  // B's segments on a uniform grid; every segment of A tests the cells within c of its bbox
  const ext = Math.max(bx1 - bx0, by1 - by0, 1e-9)
  const cell = Math.max(c, ext / 96, 1e-6)
  const nx = Math.max(1, Math.ceil((bx1 - bx0) / cell) + 1)
  const ny = Math.max(1, Math.ceil((by1 - by0) / cell) + 1)
  const grid = new Map<number, number[]>()
  const M = B.ax.length
  const cx = (x: number) => Math.min(nx - 1, Math.max(0, Math.floor((x - bx0) / cell)))
  const cy = (y: number) => Math.min(ny - 1, Math.max(0, Math.floor((y - by0) / cell)))
  for (let g = 0; g < M; g++) {
    const i0 = cx(Math.min(B.ax[g], B.bx[g]))
    const i1 = cx(Math.max(B.ax[g], B.bx[g]))
    const j0 = cy(Math.min(B.ay[g], B.by[g]))
    const j1 = cy(Math.max(B.ay[g], B.by[g]))
    for (let j = j0; j <= j1; j++)
      for (let i = i0; i <= i1; i++) {
        const k = j * nx + i
        const l = grid.get(k)
        if (l) l.push(g)
        else grid.set(k, [g])
      }
  }
  const c2 = c * c
  const stamp = new Int32Array(M)
  let tick = 0
  for (let a = 0; a < A.ax.length; a++) {
    const sx0 = Math.min(A.ax[a], A.bx[a]) - c
    const sx1 = Math.max(A.ax[a], A.bx[a]) + c
    const sy0 = Math.min(A.ay[a], A.by[a]) - c
    const sy1 = Math.max(A.ay[a], A.by[a]) + c
    if (sx1 < bx0 || sx0 > bx1 || sy1 < by0 || sy0 > by1) continue
    tick++
    for (let j = cy(sy0); j <= cy(sy1); j++)
      for (let i = cx(sx0); i <= cx(sx1); i++) {
        const l = grid.get(j * nx + i)
        if (!l) continue
        for (const g of l) {
          if (stamp[g] === tick) continue
          stamp[g] = tick
          if (segSegDist2(A.ax[a], A.ay[a], A.bx[a], A.by[a], B.ax[g], B.ay[g], B.bx[g], B.by[g]) <= c2) return true
        }
      }
  }
  for (const p of A.parts) for (const r of p.rings) if (insidePlaced(B, r[0], r[1])) return true
  for (const p of B.parts) for (const r of p.rings) if (insidePlaced(A, r[0], r[1])) return true
  return false
}

/** What the overlap lists need of a layer: its footprint (null = unknown: overlaps every layer) and placement. */
export interface LayerPlacement {
  key: string
  fp: Footprint | null
  a: number
  ox: number
  oy: number
}

const pairCache = new Map<string, boolean>()

/**
 * For every layer i the lower layers j < i it overlaps in XY (server bis.stacking.overlap_lists): footprints closer than
 * `clearance`; an unknown footprint (null) overlaps every layer, an empty one none. Pair results are cached by footprint
 * key + placement + clearance, so slider drags that only change depth re-use them.
 */
export function overlapLists(items: LayerPlacement[], clearance: number): number[][] {
  const c = Math.max(0, clearance)
  const placed: (Placed | null | undefined)[] = items.map(() => undefined)
  const get = (i: number): Placed | null => {
    let p = placed[i]
    if (p === undefined) {
      const it = items[i]
      p = placed[i] = it.fp ? place(it.fp, it.a, it.ox, it.oy) : null
    }
    return p
  }
  const out: number[][] = []
  for (let i = 0; i < items.length; i++) {
    const low: number[] = []
    for (let j = 0; j < i; j++) {
      const A = items[i]
      const B = items[j]
      if (!A.fp || !B.fp) {
        low.push(j)
        continue
      }
      const key = `${A.key}@${A.a},${A.ox},${A.oy}|${B.key}@${B.a},${B.ox},${B.oy}|${c}`
      let hit = pairCache.get(key)
      if (hit === undefined) {
        const pa = get(i)!
        const pb = get(j)!
        hit = dwithin(pa, pb, c)
        if (pairCache.size > 4096) pairCache.clear()
        pairCache.set(key, hit)
      }
      if (hit) low.push(j)
    }
    out.push(low)
  }
  return out
}

/** The placements of a project's layers from a geometry bundle (layers without geometry: unknown footprint). */
export function layerPlacements(
  layers: Layer[],
  geometry: { layers?: Record<string, LayerGeometry> } | null | undefined,
  art: ArtTransform,
): LayerPlacement[] {
  return layers.map((l) => {
    const lg = geometry?.layers?.[l.id]
    const [a, ox, oy] = placement(art, l)
    return { key: lg ? `${lg.layerId}:${lg.hash}` : `?${l.id}`, fp: lg ? layerFootprint(lg) : null, a, ox, oy }
  })
}
