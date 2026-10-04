// The layer stack as data (PLAN §11): visible layers with their bodies' parts, ONE Principled material per shape
// (layer material ∘ Layer.elementMaterials), REAL z (depth.z + ε; camera.explode is legacy and ignored), canvas-space
// hulls for the CAD iso framing, and the two three.js rendering flags (glass through glass, translucent pass routing).
import type { ArtTransform, Layer, LayerGeometry, LayerTransform, Presets } from '../../types'
import { FLAT_MATERIAL, isTransmissive, shapeMaterial, type Principled } from '../../lib/materials3d'
import { flattenSpline } from '../geometry/heightfield'
import {
  imageQuad,
  LAYER_EPS,
  layerBodyHeight,
  layerBodyParts,
  layerDepth,
  layerScale,
  partStackPairs,
  type BodyPart,
  type DepthParams,
} from '../geometry/layerGeometry'
import { convexOverlapArea, hull2d, polyArea } from './iso'

export interface StackEntry {
  layer: Layer
  lg: LayerGeometry
  /** Stack level (index in project.layers, bottom = 0). */
  level: number
  /** World z of the layer's base (depth.z + LAYER_EPS). */
  z: number
  /** World height of its bodies (worker _body_height: thickness + 2·inflate·maxRadius·S, or the in-layer stack). */
  height: number
  scale: number
  depth: DepthParams
  parts: BodyPart[]
  /** Part-index pairs [i, j] of overlapping pieces: j is stacked on i by their real heights (worker _relations). */
  pieceStack: [number, number][]
  /** Resolved Principled params per part key (layer material ∘ elementMaterials). */
  params: Map<string, Principled>
  /** Some shape of the layer transmits (glass). */
  transmissive: boolean
  /** A transmissive layer above overlaps it: its glass is drawn opaque (no transmission through transmission). */
  glassAbove: boolean
  /** Translucent bodies go to the opaque pass (false only when refracting glass lies below and none above). */
  routeBlended: boolean
  /**
   * How much of the surface beneath this layer is lit THROUGH it (0..1): a glass body lying on the plate / a lower
   * layer is seen against light that already crossed it (Cycles traces it, caustics on), so its transmitted colour
   * takes Base Color once more; a body floating well above (gap ≳ its size) is seen against untinted light.
   */
  contact: number
  /** Canvas-space bbox and convex hull (flat x,y pairs) of the layer after transforms. */
  bbox: [number, number, number, number]
  hull: number[]
}

/**
 * Canvas-space convex hull of a layer's silhouette (+ flat raster card quads), after art ∘ layer transforms — worker
 * scene.subject_hulls: an EXTRUDED raster region is bounded by its traced contour, not its PNG placement (Find Device's
 * 1685 px image overhangs the plate: the iso frame came out 1.3–1.7 % too loose and 4.5 % off-centre).
 */
function layerHull(lg: LayerGeometry, art: ArtTransform, t: LayerTransform): number[] {
  const pts: number[] = []
  const spl = lg.silhouette?.length ? lg.silhouette : (lg.regions ?? []).flatMap((r) => r.splines)
  for (const s of spl) for (const v of flattenSpline(s, 0.004, 0)) pts.push(v)
  const regionIds = new Set((lg.regions ?? []).map((r) => r.elementId))
  for (const im of lg.images ?? []) {
    if (!im?.url || regionIds.has(im.elementId)) continue
    for (const [x, y] of imageQuad(im, lg.bbox)) pts.push(x, y)
  }
  if (!pts.length) {
    const [x0, y0, x1, y1] = lg.bbox ?? [-1, -1, 1, 1]
    pts.push(x0, y0, x1, y0, x1, y1, x0, y1)
  }
  const sa = art.scale
  const sl = t.scale
  for (let i = 0; i < pts.length; i += 2) {
    pts[i] = (pts[i] * sa + art.x) * sl + t.x
    pts[i + 1] = (pts[i + 1] * sa + art.y) * sl + t.y
  }
  return hull2d(pts)
}

const hullMemo = new WeakMap<LayerGeometry, Map<string, number[]>>()
function cachedHull(lg: LayerGeometry, art: ArtTransform, t: LayerTransform): number[] {
  const key = [art.scale, art.x, art.y, t.scale, t.x, t.y].join(',')
  let m = hullMemo.get(lg)
  if (!m) hullMemo.set(lg, (m = new Map()))
  let h = m.get(key)
  if (!h) {
    if (m.size > 16) m.clear()
    m.set(key, (h = layerHull(lg, art, t)))
  }
  return h
}

/** Visible layers with geometry, parts, shape materials and the glass-through-glass / pass routing flags. */
export function buildStack(
  layers: Layer[],
  geometry: Record<string, LayerGeometry> | null | undefined,
  art: ArtTransform,
  presets: Presets | null,
  plate = true,
): StackEntry[] {
  const out: StackEntry[] = []
  layers.forEach((layer, level) => {
    const lg = geometry?.[layer.id]
    if (!layer.visible || !lg || layer.opacity <= 0.001) return
    const t = layer.transform
    const S = layerScale(art.scale, t.scale)
    const depth = layerDepth(layer, S)
    const parts = layerBodyParts(layer, lg, depth)
    const params = new Map<string, Principled>()
    let transmissive = false
    for (const part of parts) {
      // cards: unextruded <image> art renders with the 'flat' preset unless the shape has its own material (worker)
      const m = shapeMaterial(layer, part.elementId, presets, part.card ? FLAT_MATERIAL : null)
      params.set(part.key, m.params)
      if (isTransmissive(m.params) && !part.card) transmissive = true
    }
    const hull = cachedHull(lg, art, t)
    let x0 = Infinity
    let y0 = Infinity
    let x1 = -Infinity
    let y1 = -Infinity
    for (let i = 0; i < hull.length; i += 2) {
      x0 = Math.min(x0, hull[i])
      x1 = Math.max(x1, hull[i])
      y0 = Math.min(y0, hull[i + 1])
      y1 = Math.max(y1, hull[i + 1])
    }
    out.push({
      layer,
      lg,
      level,
      z: (Number.isFinite(layer.depth.z) ? layer.depth.z : 0) + LAYER_EPS,
      height: layerBodyHeight(layer, lg, S),
      scale: S,
      depth,
      parts,
      pieceStack: partStackPairs(parts, lg, S),
      params,
      transmissive,
      glassAbove: false,
      routeBlended: true,
      contact: 0,
      bbox: [x0, y0, x1, y1],
      hull,
    })
  })
  // overlap of two layers' outlines (convex hulls; a bbox test first)
  const area = out.map((e) => Math.max(1e-12, polyArea(e.hull)))
  const shared = (i: number, j: number) => {
    const a = out[i].bbox
    const b = out[j].bbox
    if (!(a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3])) return 0
    return convexOverlapArea(out[i].hull, out[j].hull)
  }
  const overlaps = (i: number, j: number) => shared(i, j) > 1e-5
  // contact: the surface right under each layer (the plate at z = 0 or a lower layer's top), weighted by how much of
  // the layer it underlies, fading out as the gap grows to the layer's own size (the light's offset under it)
  for (let i = 0; i < out.length; i++) {
    const e = out[i]
    const size = Math.max(1e-3, 0.5 * Math.min(e.bbox[2] - e.bbox[0], e.bbox[3] - e.bbox[1]))
    const close = (gap: number) => Math.max(0, Math.min(1, 1 - Math.max(0, gap) / size))
    let c = plate ? close(e.z) : 0
    for (let j = 0; j < i; j++) c = Math.max(c, close(e.z - (out[j].z + out[j].height)) * Math.min(1, shared(i, j) / area[i]))
    e.contact = c
  }
  for (let i = 0; i < out.length; i++) {
    for (let j = i + 1; j < out.length && !out[i].glassAbove; j++)
      if (out[j].transmissive && overlaps(i, j)) out[i].glassAbove = true
  }
  // Pass routing of translucent bodies: refracting glass = transmissive and not drawn opaque (covered).
  const refracts = (e: StackEntry) => e.transmissive && !e.glassAbove
  for (let i = 0; i < out.length; i++) {
    let above = false
    let below = false
    for (let j = 0; j < out.length && !above; j++) {
      if (j === i || !refracts(out[j]) || !overlaps(i, j)) continue
      if (j > i) above = true
      else below = true
    }
    out[i].routeBlended = above || !below
  }
  return out
}

/** The plate as framing geometry: its outline (canvas space) over [−thickness, 0]. */
export interface PlateFrame {
  outline: [number, number][]
  thickness: number
}

/**
 * World-space points whose hull bounds what the camera must show (worker subject_points): the plate outline at its
 * front and back faces — or, without a plate, the canvas square at z = 0 — and every layer's hull at its real z span.
 * Written into `out` (xyz triplets); returns the number of points.
 */
export function stackFramePoints(stack: StackEntry[], plate: PlateFrame | null, out: number[]): number {
  let n = 0
  const push = (x: number, y: number, z: number) => {
    out[n * 3] = x
    out[n * 3 + 1] = y
    out[n * 3 + 2] = z
    n++
  }
  if (plate) {
    for (const [x, y] of plate.outline) {
      push(x, y, 0)
      push(x, y, -plate.thickness)
    }
  } else {
    push(-1, -1, 0)
    push(1, -1, 0)
    push(1, 1, 0)
    push(-1, 1, 0)
  }
  for (const e of stack) {
    const z0 = e.z
    const z1 = e.z + e.height
    for (let i = 0; i < e.hull.length; i += 2) {
      push(e.hull[i], e.hull[i + 1], z0)
      push(e.hull[i], e.hull[i + 1], z1)
    }
  }
  out.length = n * 3
  return n
}

export function stackTop(stack: StackEntry[]): number {
  let top = 0
  for (const e of stack) top = Math.max(top, e.z + e.height)
  return top
}

