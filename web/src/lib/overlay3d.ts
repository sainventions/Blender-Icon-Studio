// Live-view twin of blender_worker/overlay.py (round 5) + the scene.py rules that use it:
//
// · flush edges — Liquid Glass art whose outline runs along the plate outline (Classroom's / CRD's frames, DJI's body)
//   has no rim / sheen / glow / clear pill edge along it (flushSpec → materials3d BIS_FLUSH);
// · display-space blend films — translucent, solid-painted Liquid Glass pieces of the light / dark renditions blend
//   like the SVG through the colour mode's view transform: out = T · R_beneath + E per channel, (T, E) the tangent of
//   the exact display-space blend at an estimate of what lies beneath (beneathSrgb → blendCoeffs → filmParams; drawn
//   by materials3d BIS_FILM with blend colour T);
// · thin strokes (scene.THIN_RATIO / THIN_SOLID) keep their Liquid Glass body across the bevel ('solid_edge').
//
// Pure functions (no three.js), unit-tested against the worker sources in viewport-sync.test.mjs.
import type { Fill, Layer, LayerGeometry, Paint, PlateShape, Project, Spline } from '../types'
import { BRAND_CAP, BRAND_KNEE, neutralToneMap, softClip, softClipInverse } from '../viewport/scene/displayTransform'
import { displayPaint, THIN_RATIO, THIN_SOLID, type FilmParams, type FlushSpec, type RGB } from './materials3d'

/** overlay.FILM_MODES: colour modes whose translucent Liquid Glass pieces render as films. */
export const FILM_MODES = ['brand', 'neutral', 'standard'] as const
/** overlay.FILM_FOLLOW: share of the beneath's deviation from the estimate a film passes on. */
export const FILM_FOLLOW = 0.55
/** overlay.SAMPLES: sample grid per side of a piece's bbox for the beneath estimate. */
export const SAMPLES = 14
/** overlay.FLUSH_TOL: outline points this close to the plate outline (canvas units) are flush with it. */
export const FLUSH_TOL = 0.006

type Pt = [number, number]
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

// ------------------------------------------------------------------------------------------------ colour
const eotf1 = (s: number) => {
  const c = clamp(s, 0, 1)
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}
const oetf1 = (x: number) => {
  const c = clamp(x, 0, 1)
  return c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055
}
const eotf = (c: RGB) => c.map(eotf1) as RGB
const oetf = (c: RGB) => c.map(oetf1) as RGB

/** overlay._transform: (forward radiance → display-linear, inverse display-linear → radiance) of a colour mode. */
function transformOf(cm: string, knee = BRAND_KNEE): [(r: RGB) => RGB, (d: RGB) => RGB] {
  if (cm === 'brand') return [(r) => softClip(r, knee), (d) => softClipInverse(d, knee, BRAND_CAP)]
  if (cm === 'neutral')
    return [
      (r) => neutralToneMap(r.map((v) => Math.max(v, 0)) as RGB).map((v) => clamp(v, 0, 1)) as RGB,
      (d) => displayPaint(d, 1),
    ]
  return [(r) => r.map((v) => clamp(v, 0, 1)) as RGB, (d) => d.map((v) => clamp(v, 0, 1)) as RGB]
}

/**
 * overlay.blend_coeffs: (T, E) per channel (radiance) such that T · R_B + E displays, through colour mode `cm`, the SVG
 * blend (1 − a)·s_B + a·s_P — exactly at the beneath estimate, to first order around it (`follow` < 1: that share of
 * the slope only, the rest folded into E).
 */
export function blendCoeffs(paint: RGB, alpha: number, beneath: RGB, cm = 'brand', knee = BRAND_KNEE, follow = 1): FilmParams {
  const a = clamp(alpha, 0, 1)
  const sp = paint.map((v) => clamp(v, 0, 1)) as RGB
  const sb = beneath.map((v) => clamp(v, 0, 1)) as RGB
  const [fwd, inv] = transformOf(cm, knee)
  const h = (r: RGB): RGB => {
    const s = oetf(fwd(r))
    return inv(eotf(s.map((v, i) => (1 - a) * v + a * sp[i]) as RGB))
  }
  const r0 = inv(eotf(sb))
  const h0 = h(r0)
  const dr = r0.map((v) => Math.max(v * 0.02, 2e-4)) as RGB
  const h1 = h(r0.map((v, i) => v + dr[i]) as RGB) // PBR Neutral couples the channels: perturbed together
  const f = clamp(follow, 0, 1)
  const t = h1.map((v, i) => clamp((v - h0[i]) / dr[i], 0, 1) * f) as RGB
  const e = h0.map((v, i) => Math.max(v - t[i] * r0[i], 0)) as RGB
  return { t, e }
}

/** overlay.film_params: (T, E) of a translucent piece (beneath null: a mid-grey estimate), FILM_FOLLOW of the slope. */
export function filmParams(paint: RGB, alpha: number, beneath: RGB | null, cm: string): FilmParams {
  return blendCoeffs(paint, alpha, beneath ?? [0.5, 0.5, 0.5], cm, BRAND_KNEE, FILM_FOLLOW)
}

// ------------------------------------------------------------------------------------------------ paint
function hexSrgb(hex: string | undefined): RGB {
  let h = String(hex ?? '#000000').trim().replace('#', '')
  if (h.length === 3)
    h = h
      .split('')
      .map((c) => c + c)
      .join('')
  const n = parseInt(h.slice(0, 6), 16)
  if (Number.isNaN(n)) return [0, 0, 0]
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255]
}

function interp(x: number, xs: number[], ys: number[]): number {
  if (x <= xs[0]) return ys[0]
  for (let i = 1; i < xs.length; i++) if (x <= xs[i]) return xs[i] - xs[i - 1] > 0 ? ys[i - 1] + ((ys[i] - ys[i - 1]) * (x - xs[i - 1])) / (xs[i] - xs[i - 1]) : ys[i]
  return ys[ys.length - 1]
}

type Stopped = { stops?: { offset: number; color: string; opacity?: number }[] }
function ramp(paint: Stopped, t: number): [RGB, number] {
  const st = [...(paint.stops ?? [])].sort((a, b) => (a.offset ?? 0) - (b.offset ?? 0))
  if (!st.length) return [[1, 1, 1], 1]
  const off = st.map((s) => clamp(s.offset ?? 0, 0, 1))
  const col = st.map((s) => hexSrgb(s.color))
  const op = st.map((s) => (s.opacity ?? 1))
  const tt = clamp(t, 0, 1)
  return [[0, 1, 2].map((k) => interp(tt, off, col.map((c) => c[k]))) as RGB, interp(tt, off, op)] // SVG: sRGB
}

/** overlay.paint_at: sRGB colour + opacity of a Fill / Paint at a point (its own coordinate space). */
export function paintAt(paint: Fill | Paint | null | undefined, p: Pt, bbox: [number, number, number, number] = [-1, -1, 1, 1]): [RGB, number] {
  const t = paint?.type ?? 'solid'
  if (t === 'solid') {
    const s = paint as { color?: string; opacity?: number }
    return [hexSrgb(s.color ?? '#ffffff'), s.opacity ?? 1]
  }
  if (t === 'system-light' || t === 'system-dark') {
    const [a, b] = t === 'system-light' ? ['#ffffff', '#e4e5ea'] : ['#3a3a3f', '#111114']
    return paintAt({ type: 'linear', start: [0, bbox[3]], end: [0, bbox[1]], stops: [{ offset: 0, color: a, opacity: 1 }, { offset: 1, color: b, opacity: 1 }] }, p, bbox)
  }
  if (t === 'linear') {
    const g = paint as { start?: Pt; end?: Pt } & Stopped
    const [sx, sy] = g.start ?? [0, 1]
    const [ex, ey] = g.end ?? [0, -1]
    const dx = ex - sx
    const dy = ey - sy
    const l2 = dx * dx + dy * dy || 1e-9
    return ramp(g, ((p[0] - sx) * dx + (p[1] - sy) * dy) / l2)
  }
  if (t === 'radial') {
    const g = paint as { center?: Pt; radius?: number; matrix?: number[] | null } & Stopped
    const [cx, cy] = g.center ?? [0, 0]
    const r = g.radius || 1e-9
    let q: Pt = p
    if (g.matrix && g.matrix.length === 6) {
      const [a, b, c, d, e, f] = g.matrix
      const det = a * d - b * c || 1e-12
      const x = p[0] - e
      const y = p[1] - f
      q = [(d * x - c * y) / det, (-b * x + a * y) / det]
    }
    return ramp(g, Math.hypot(q[0] - cx, q[1] - cy) / r)
  }
  return [[0.5, 0.5, 0.5], 0]
}

// ------------------------------------------------------------------------------------------------ geometry
/** overlay._flatten / geometry._flatten_ring: a closed bezier ring sampled `n` times per segment. */
export function flattenRing(points: Spline['points'], n = 4): Pt[] {
  const ring: Pt[] = []
  const m = points.length
  for (let i = 0; i < m; i++) {
    const a = points[i]
    const b = points[(i + 1) % m]
    const p0 = a.co
    const c1 = a.hr ?? a.co
    const c2 = b.hl ?? b.co
    const p1 = b.co
    for (let j = 0; j < n; j++) {
      const t = j / n
      const mt = 1 - t
      ring.push([
        mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
        mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1],
      ])
    }
  }
  return ring
}

/** overlay._inside: even-odd point-in-polygon against closed rings. */
export function insideRings(rings: Pt[][], p: Pt): boolean {
  let inside = false
  for (const r of rings) {
    if (r.length < 3) continue
    for (let i = 0; i < r.length; i++) {
      const [x0, y0] = r[i]
      const [x1, y1] = r[(i + 1) % r.length]
      if (y0 > p[1] !== y1 > p[1]) {
        const dy = Math.abs(y1 - y0) < 1e-12 ? 1e-12 : y1 - y0
        if (p[0] < x0 + ((p[1] - y0) * (x1 - x0)) / dy) inside = !inside
      }
    }
  }
  return inside
}

/** overlay.plate_mask. */
export function plateMask(shape: PlateShape | string, cornerRadius: number, p: Pt): boolean {
  const x = Math.abs(p[0])
  const y = Math.abs(p[1])
  if (shape === 'circle') return x * x + y * y <= 1
  if (shape === 'squircle') return x ** 5 + y ** 5 <= 1
  if (shape === 'rounded') {
    const r = clamp(2 * cornerRadius, 0, 1)
    const qx = Math.max(x - (1 - r), 0)
    const qy = Math.max(y - (1 - r), 0)
    return x <= 1 && y <= 1 && qx * qx + qy * qy <= r * r + 1e-12
  }
  if (shape === 'square') return x <= 1 && y <= 1
  return false
}

/** overlay.plate_distance: approximate signed distance (+ inside) to the plate outline (materials._flush_keep's field). */
export function plateDistance(shape: PlateShape | string, cornerRadius: number, p: Pt, grow = 1): number {
  const x = Math.abs(p[0]) / Math.max(grow, 1e-6)
  const y = Math.abs(p[1]) / Math.max(grow, 1e-6)
  let d: number
  if (shape === 'circle') d = 1 - Math.hypot(x, y)
  else if (shape === 'square') d = 1 - Math.max(x, y)
  else if (shape === 'rounded') {
    const r = clamp(2 * cornerRadius, 0, 1)
    const qx = x - (1 - r)
    const qy = y - (1 - r)
    d = r - (Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0))
  } else d = 1 - (x ** 5 + y ** 5) ** 0.2
  return d * grow
}

/** overlay.flush_spec: the materials 'flush' spec when a layer's outline runs along the plate outline, else null. */
export function flushSpec(ringsCanvas: Pt[][], shape: PlateShape | string, cornerRadius: number, bevel: number, grow = 1): FlushSpec | null {
  if (shape === 'none' || !ringsCanvas.length) return null
  const pts = ringsCanvas.flat()
  if (!pts.length) return null
  let near = 0
  for (const p of pts) if (Math.abs(plateDistance(shape, cornerRadius, p, grow)) < FLUSH_TOL) near++
  if (near < Math.max(6, 0.02 * pts.length)) return null
  const b = Math.max(0.004, bevel)
  const r5 = (v: number) => Math.round(v * 1e5) / 1e5
  return { shape, r: clamp(2 * cornerRadius, 0, 1), grow, band: [r5(0.5 * b), r5(1.3 * b + 0.004)] }
}

/** Canvas = art · k + off for a layer (art.scale × transform.scale, then the transform's offset). */
function toCanvas(art: Project['canvas']['art'], tr: Layer['transform'] | undefined): [number, Pt] {
  const sa = art?.scale ?? 1
  const sl = tr?.scale ?? 1
  return [sa * sl, [(art?.x ?? 0) * sl + (tr?.x ?? 0), (art?.y ?? 0) * sl + (tr?.y ?? 0)]]
}

/** The effective preset of a layer (scene.py presets_eff: Effects off → flat). */
const effPreset = (L: Layer) => (L.glass === false ? 'flat' : L.material?.preset)

/**
 * scene._flush_params: {layer id → flush spec} for (effective) Liquid Glass layers whose outline runs along the plate
 * outline. `plateShape` = the plate shape when the plate is drawn (visible, fill not 'none'), else 'none'.
 */
export function flushParams(
  layers: Layer[],
  geometry: Record<string, LayerGeometry> | null | undefined,
  canvas: Project['canvas'],
  plateShape: PlateShape | string,
): Map<string, FlushSpec> {
  const out = new Map<string, FlushSpec>()
  if (plateShape === 'none') return out
  const sa = canvas.art?.scale ?? 1
  for (const L of layers) {
    const g = geometry?.[L.id]
    if (!g || effPreset(L) !== 'liquid_glass') continue
    const [k, off] = toCanvas(canvas.art, L.transform)
    const spl = g.silhouette?.length ? g.silhouette : (g.regions ?? []).flatMap((r) => r.splines ?? [])
    const rings = spl
      .filter((s) => (s.points ?? []).length >= 2)
      .map((s) => flattenRing(s.points, 6).map(([x, y]) => [x * k + off[0], y * k + off[1]] as Pt))
    const bevel = Math.min(L.depth?.bevel ?? 0.045, 0.9 * (g.safeRadius ?? 1) * sa * (L.transform?.scale ?? 1))
    const fs = flushSpec(rings, plateShape, canvas.cornerRadius ?? 0.225, bevel)
    if (fs) out.set(L.id, fs)
  }
  return out
}

/** scene.py 'solid_edge': THIN_SOLID when the layer's (local) bevel ≥ THIN_RATIO × its safe radius (thin strokes). */
export function solidEdge(bevelLocal: number, safeRadius: number | null | undefined): number {
  return bevelLocal >= THIN_RATIO * (safeRadius ?? 1) ? THIN_SOLID : 0
}

/**
 * overlay.beneath_srgb: mean sRGB of what the SVG shows beneath region `regionIndex` of layer `layerId` (its interior
 * sampled on a SAMPLES² grid): plate fill + lower visible layers + lower regions of the same layer, composited like the
 * SVG (sRGB, region × layer opacity). Null when nothing lies beneath.
 */
export function beneathSrgb(
  project: Project,
  geometry: Record<string, LayerGeometry>,
  layerId: string,
  regionIndex: number,
  shape?: PlateShape | string,
  samples = SAMPLES,
): RGB | null {
  const canvas = project.canvas
  const layers = project.layers.filter((L) => L.visible)
  const idx = layers.findIndex((L) => L.id === layerId)
  if (idx < 0) return null
  const me = layers[idx]
  const regions = geometry[layerId]?.regions ?? []
  if (regionIndex < 0 || regionIndex >= regions.length) return null
  const [k, off] = toCanvas(canvas.art, me.transform)
  const rings = (regions[regionIndex].splines ?? [])
    .filter((s) => (s.points ?? []).length >= 2)
    .map((s) => flattenRing(s.points).map(([x, y]) => [x * k + off[0], y * k + off[1]] as Pt))
    .filter((r) => r.length >= 3)
  if (!rings.length) return null
  const all = rings.flat()
  const x0 = Math.min(...all.map((p) => p[0]))
  const x1 = Math.max(...all.map((p) => p[0]))
  const y0 = Math.min(...all.map((p) => p[1]))
  const y1 = Math.max(...all.map((p) => p[1]))
  const lin = (a: number, b: number) => Array.from({ length: samples }, (_, i) => a + ((b - a) * (i + 1)) / (samples + 1))
  let pts: Pt[] = []
  for (const gy of lin(y0, y1)) for (const gx of lin(x0, x1)) if (insideRings(rings, [gx, gy])) pts.push([gx, gy])
  if (pts.length < 3) pts = [[all.reduce((s, p) => s + p[0], 0) / all.length, all.reduce((s, p) => s + p[1], 0) / all.length]]
  const sh = shape ?? canvas.shape ?? 'squircle'
  const plate = canvas.plate
  const col: (RGB | null)[] = pts.map(() => null)
  if (plate?.visible !== false && sh !== 'none' && plate?.fill?.type !== 'none') {
    const fill = plate?.fill ?? { type: 'solid', color: '#ffffff', opacity: 1 }
    pts.forEach((p, i) => {
      if (plateMask(sh, canvas.cornerRadius ?? 0.225, p)) col[i] = paintAt(fill.type === 'auto' ? { type: 'solid', color: '#ffffff', opacity: 1 } : fill, p)[0]
    })
  }
  for (let li = 0; li <= idx; li++) {
    const L = layers[li]
    const gl = geometry[L.id]
    const fill = L.fill ?? { type: 'auto' }
    if (fill.type === 'none') continue
    const [kl, ol] = toCanvas(canvas.art, L.transform)
    const lop = L.opacity ?? 1
    let regs = gl?.regions ?? []
    if (L.id === layerId) regs = regs.slice(0, regionIndex)
    const artPts = pts.map(([x, y]) => [(x - ol[0]) / Math.max(kl, 1e-9), (y - ol[1]) / Math.max(kl, 1e-9)] as Pt)
    for (const r of regs) {
      const rr = (r.splines ?? []).filter((s) => (s.points ?? []).length >= 2).map((s) => flattenRing(s.points)).filter((q) => q.length >= 3)
      const paint = fill.type !== 'auto' ? fill : r.paint
      artPts.forEach((ap, i) => {
        if (!insideRings(rr, ap)) return
        const [pc, po] = paintAt(paint, ap, (gl?.bbox as [number, number, number, number]) ?? [-1, -1, 1, 1])
        const a = clamp(po * (r.opacity ?? 1) * lop, 0, 1)
        const below = col[i] ?? pc
        col[i] = below.map((v, c) => (1 - a) * v + a * pc[c]) as RGB
      })
    }
  }
  const ok = col.filter((c): c is RGB => c !== null)
  if (!ok.length) return null
  return [0, 1, 2].map((c) => ok.reduce((s, v) => s + v[c], 0) / ok.length) as RGB
}

/**
 * scene._film_params: {`${layerId}:${regionIndex}` → (T, E)} for translucent, solid-painted (effective) Liquid Glass
 * pieces of the light / dark renditions in the FILM_MODES. A layer with a translucent piece the film cannot express
 * (gradient paint, a raster under a fill override) keeps the alpha model for all its pieces; combined bodies, touching
 * opaque pieces and raster pieces never film.
 */
export function filmParamsFor(
  project: Project,
  geometry: Record<string, LayerGeometry> | null | undefined,
  colorMode: string,
  mono: boolean,
  touching: (g: LayerGeometry) => boolean,
  plateShape?: PlateShape | string,
): Map<string, FilmParams> {
  const out = new Map<string, FilmParams>()
  if (mono || !geometry || !(FILM_MODES as readonly string[]).includes(colorMode)) return out
  const cands: [string, number, string, number][] = []
  for (const L of project.layers) {
    if (!L.visible) continue
    const g = geometry[L.id]
    const fill = L.fill ?? { type: 'auto' }
    if (!g || effPreset(L) !== 'liquid_glass' || L.mode === 'combined' || (fill.type !== 'auto' && fill.type !== 'solid') || touching(g)) continue
    const imgs = new Set((g.images ?? []).filter((im) => im && (im.url || im.path)).map((im) => im.elementId))
    const lop = L.opacity ?? 1
    const mine: [string, number, string, number][] = []
    let blocked = false
    ;(g.regions ?? []).forEach((r, i) => {
      const op = (r.opacity ?? 1) * lop
      if (op >= 0.999) return
      if (imgs.has(r.elementId) && fill.type === 'auto') return // a raster piece: its own (alpha) material
      const paint = fill.type === 'solid' ? fill : r.paint
      if (!imgs.has(r.elementId) && paint?.type === 'solid') mine.push([L.id, i, paint.color ?? '#ffffff', op])
      else blocked = true
    })
    if (!blocked) cands.push(...mine)
  }
  for (const [lid, i, colour, op] of cands) {
    let below: RGB | null = null
    try {
      below = beneathSrgb(project, geometry, lid, i, plateShape)
    } catch {
      below = null // a heuristic never breaks a render
    }
    out.set(`${lid}:${i}`, filmParams(hexSrgb(colour), op, below, colorMode))
  }
  return out
}
