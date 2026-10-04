// Layer / plate bodies (PLAN §11): every piece, silhouette, raster contour, card and the plate is a height-field body
// (./heightfield.ts, the port of blender_worker/heightfield.py), memoised by LayerGeometry.hash + depth params and
// disposed when no mounted component uses it any more.
//
// Units mirror the worker (scene.py `_layer`): a layer's bodies live in ART-LOCAL units under one uniform scale
// S = canvas.art.scale × layer.transform.scale, built with thickness / S and bevel / S (bevel = min(bevel, t/2): thin
// parts taper by construction, so there is no safe-radius clamp), centred on their mid-plane. Each body sits at local
// z = (thickness / 2 + zSub) / S + lift, where lift = max(0, half − thickness / 2) keeps an inflated layer's lowest
// point on the layer's z.
import * as THREE from 'three'
import type { Layer, LayerGeometry, Plate, PlateShape, RasterCard, Spline, Vec2 } from '../../types'
import { hasOwnMaterials, type PaintUv } from '../../lib/materials3d'
import { plateOutline } from '../../lib/shapes'
import { RefCache } from '../refCache'
import { buildBody, halfHeight, inradius } from './heightfield'

export interface Body {
  geometry: THREE.BufferGeometry
  /** Max top height (local units) = the body's half height. */
  half: number
}

export const bodyCache = new RefCache<string, Body>((b) => b.geometry.dispose())

/** Smallest layer scale (the worker clamps the same way; a zero scale would make the normal matrix singular). */
export const MIN_LAYER_SCALE = 1e-4
/** Layer back face above the plate's front face (worker LAYER_EPS). */
export const LAYER_EPS = 0.002
/** zSub step: zSub ≥ 1 is a sub-layer index (worker REGION_DZ). */
export const REGION_DZ = 0.001
/** Flat raster cards (worker CARD_THICKNESS), centred 0.0005 above the layer's z. */
export const CARD_THICKNESS = 0.004

export interface DepthParams {
  /** World thickness (= layer.depth.thickness). */
  worldThickness: number
  /** Local (art-unit) thickness = world thickness / S. */
  thickness: number
  /** Local round-edge radius b = min(bevel, thickness / 2) / S. */
  bevel: number
  inflate: number
  segments: number
  /** Local → world uniform scale S. */
  scale: number
}

const r5 = (v: number) => Math.round(v * 1e5) / 1e5
const num = (v: unknown, d: number) => (Number.isFinite(v as number) ? (v as number) : d)

/** S = canvas.art.scale × layer.transform.scale (clamped like the worker). */
export function layerScale(artScale: number, layerScaleValue: number): number {
  return Math.max(MIN_LAYER_SCALE, num(artScale, 1) * num(layerScaleValue, 1))
}

/** Worker scene._layer depth handling in local units. */
export function layerDepth(layer: Layer, scale: number): DepthParams {
  const S = Math.max(MIN_LAYER_SCALE, scale)
  const t = Math.max(0, num(layer.depth.thickness, 0.1))
  return {
    worldThickness: t,
    thickness: t / S,
    bevel: Math.min(Math.max(0, num(layer.depth.bevel, 0.045)), t / 2) / S,
    inflate: Math.max(0, Math.min(1, num(layer.depth.inflate, 0))),
    segments: Math.max(1, Math.trunc(num(layer.depth.bevelSegments, 6))),
    scale: S,
  }
}

function depthKey(d: DepthParams): string {
  return `T${r5(d.thickness)}B${r5(d.bevel)}K${r5(d.inflate)}N${d.segments}S${r5(d.scale)}`
}

export function bodyFromSplines(splines: Spline[], thickness: number, bevel: number, inflate: number, segments: number, scale: number): Body {
  try {
    const arr = buildBody(splines, thickness, bevel, inflate, segments, scale)
    const g = new THREE.BufferGeometry()
    if (!arr) return { geometry: g, half: 0 }
    g.setAttribute('position', new THREE.BufferAttribute(arr.position, 3))
    g.setAttribute('normal', new THREE.BufferAttribute(arr.normal, 3))
    g.setAttribute('uv', new THREE.BufferAttribute(arr.uv, 2))
    g.computeBoundingSphere()
    g.computeBoundingBox()
    g.userData.info = arr.info
    return { geometry: g, half: arr.info.half }
  } catch (err) {
    console.warn('[viewport] body build failed', err)
    return { geometry: new THREE.BufferGeometry(), half: 0 }
  }
}

/** A polygon as a straight-segment spline (handles on the knots). */
export function polySpline(points: Vec2[]): Spline {
  return { closed: true, hole: false, parent: -1, depth: 0, points: points.map((p) => ({ co: p, hl: p, hr: p })) }
}

export interface BodyPart {
  key: string
  /** Element id whose Layer.elementMaterials entry applies ('body' for a merged silhouette, the image id for cards). */
  elementId: string
  /** Region index in LayerGeometry.regions (individual pieces), null for the silhouette body / cards. */
  regionIndex: number | null
  opacity: number
  /** Local z of the body's mid-plane before the layer lift: (thickness / 2 + zSub) / S. */
  z: number
  /** Raster image painting this piece with its own PNG + placement (extruded contour or flat card). */
  image: RasterCard | null
  /** Flat card (no lift; its own fixed thickness). */
  card: boolean
  build: () => Body
}

/** Worker REGION_DZ handling: zSub < 1 is a world offset, zSub ≥ 1 a sub-layer index (capped at 40). */
export function regionOffset(zSub: number | null | undefined): number {
  const z = num(zSub, 0)
  return z >= 1 ? Math.min(z, 40) * REGION_DZ : z
}

/**
 * Worker scene.touching_opaque: some regions of the layer share edges (the union silhouette has fewer outer contours
 * than the regions together) and every region is opaque vector paint with no sub-layer offset — the layer then renders
 * as ONE body painted by the layer texture.
 */
export function touchingOpaque(lg: LayerGeometry | null | undefined): boolean {
  const regions = lg?.regions ?? []
  const sil = lg?.silhouette ?? []
  if (regions.length < 2 || !sil.length) return false
  if ((lg?.images ?? []).length) return false
  if (regions.some((r) => num(r.opacity, 1) < 0.999 || num(r.zSub, 0) !== 0)) return false
  if (regions.some((r) => (r.paint.type === 'linear' || r.paint.type === 'radial') && r.paint.stops.some((s) => num(s.opacity, 1) < 0.999)))
    return false
  const silOuter = sil.filter((s) => !s.hole).length
  const regOuter = regions.reduce((n, r) => n + (r.splines ?? []).filter((s) => !s.hole).length, 0)
  return silOuter > 0 && silOuter < regOuter
}

/**
 * One body (silhouette) instead of one per region: layer mode 'combined', or touching opaque pieces — unless a shape of
 * the layer has its own material (Layer.elementMaterials), which needs its own body.
 */
export function isCombinedBody(layer: Layer, lg: LayerGeometry): boolean {
  if (layer.mode === 'combined') return true
  return touchingOpaque(lg) && !hasOwnMaterials(layer, (lg.regions ?? []).map((r) => r.elementId))
}

/** Worker image_uv: affine art → image UV of a raster image (its full placement, else its bbox). */
export function imageUv(card: RasterCard, fallback: [number, number, number, number] = [-1, -1, 1, 1]): PaintUv {
  const { matrix: m, width: W, height: H } = card
  if (m && m.length === 6 && W && H && W > 0 && H > 0 && m.every(Number.isFinite)) {
    const [a, b, c, d, e, f] = m
    const det = a * d - b * c
    if (Math.abs(det) > 1e-18) {
      const pxx = d / det
      const pxy = -c / det
      const pxc = (c * f - d * e) / det
      const pyx = -b / det
      const pyy = a / det
      const pyc = (b * e - a * f) / det
      return [pxx / W, pxy / W, pxc / W, -pyx / H, -pyy / H, 1 - pyc / H]
    }
  }
  const [x0, y0, x1, y1] = card.bbox ?? fallback
  const w = Math.max(1e-6, x1 - x0)
  const h = Math.max(1e-6, y1 - y0)
  return [1 / w, 0, -x0 / w, 0, 1 / h, -y0 / h]
}

/** Worker image_quad: art-space corners (CCW) of a raster card's full placement (matrix) or its bbox. */
export function imageQuad(card: RasterCard, fallback: [number, number, number, number] = [-1, -1, 1, 1]): Vec2[] {
  const { matrix: m, width: W, height: H } = card
  if (m && m.length === 6 && W && H && m.every(Number.isFinite)) {
    const [a, b, c, d, e, f] = m
    const pts: Vec2[] = (
      [
        [0, H],
        [W, H],
        [W, 0],
        [0, 0],
      ] as Vec2[]
    ).map(([px, py]) => [a * px + c * py + e, b * px + d * py + f])
    let area2 = 0
    for (let i = 0; i < 4; i++) {
      const p = pts[(i + 3) % 4]
      const q = pts[i]
      area2 += p[0] * q[1] - q[0] * p[1]
    }
    if (Math.abs(area2) > 1e-12) return area2 > 0 ? pts : pts.reverse()
  }
  const [x0, y0, x1, y1] = card.bbox ?? fallback
  return [
    [x0, y0],
    [x1, y0],
    [x1, y1],
    [x0, y1],
  ]
}

/** The bodies of one layer: one per region ('individual') or the union silhouette, plus flat raster cards. */
export function layerBodyParts(layer: Layer, lg: LayerGeometry, d: DepthParams): BodyPart[] {
  const dk = depthKey(d)
  const base = `${lg.layerId}:${lg.hash}`
  const silhouette = lg.silhouette ?? []
  const regions = lg.regions ?? []
  const S = d.scale
  const mid = d.worldThickness / 2
  const autoPaint = (layer.fill?.type ?? 'auto') === 'auto'
  const images = new Map<string, RasterCard>()
  for (const im of lg.images ?? []) if (im && im.url) images.set(im.elementId, im)
  const parts: BodyPart[] = []
  if (silhouette.length || regions.length) {
    if (isCombinedBody(layer, lg) || !regions.length) {
      const splines = silhouette.length ? silhouette : regions.flatMap((r) => r.splines)
      parts.push({
        key: `${base}|sil|${dk}`,
        elementId: 'body',
        regionIndex: null,
        opacity: 1,
        z: mid / S,
        image: null,
        card: false,
        build: () => bodyFromSplines(splines, d.thickness, d.bevel, d.inflate, d.segments, S),
      })
    } else {
      regions.forEach((region, i) => {
        parts.push({
          key: `${base}|r${i}:${region.elementId}|${dk}`,
          elementId: region.elementId || `r${i}`,
          regionIndex: i,
          opacity: num(region.opacity, 1),
          z: (mid + regionOffset(region.zSub)) / S,
          image: autoPaint ? (images.get(region.elementId) ?? null) : null,
          card: false,
          build: () => bodyFromSplines(region.splines ?? [], d.thickness, d.bevel, d.inflate, d.segments, S),
        })
      })
    }
  }
  const covered = new Set(regions.map((r) => r.elementId))
  for (const im of images.values()) {
    if (covered.has(im.elementId)) continue
    const quad = imageQuad(im, lg.bbox)
    parts.push({
      key: `${base}|card:${im.elementId}:${quad.flat().map(r5).join(',')}|S${r5(S)}`,
      elementId: im.elementId,
      regionIndex: null,
      opacity: num(im.opacity, 1),
      z: (CARD_THICKNESS / 2 + 0.0005) / S,
      image: im,
      card: true,
      build: () => bodyFromSplines([polySpline(quad)], CARD_THICKNESS / S, 0, 0, 1, S),
    })
  }
  return parts
}

/** Lift of a layer's bodies (local): an inflated layer's lowest point stays on its z (worker scene._layer). */
export function layerLift(bodies: { part: BodyPart; body: Body }[], d: DepthParams): number {
  let half = 0
  for (const { part, body } of bodies) if (!part.card) half = Math.max(half, body.half)
  return Math.max(0, half - d.thickness / 2)
}

const inradiusCache = new Map<string, number>()
/**
 * World height of a layer's bodies (worker scene._body_height): its thickness, or twice the half height of an inflated
 * body (the silhouette's inradius bounds every piece's).
 */
export function layerBodyHeight(layer: Layer, lg: LayerGeometry, S: number): number {
  const t = Math.max(0, num(layer.depth.thickness, 0.1))
  const k = Math.max(0, num(layer.depth.inflate, 0))
  if (k <= 0) return t
  const key = `${lg.layerId}:${lg.hash}:${r5(S)}`
  let D = inradiusCache.get(key)
  if (D === undefined) {
    const spl = lg.silhouette?.length ? lg.silhouette : (lg.regions ?? []).flatMap((r) => r.splines)
    D = inradius(spl, S) * S
    if (inradiusCache.size > 256) inradiusCache.clear()
    inradiusCache.set(key, D)
  }
  const b = Math.min(Math.max(0, num(layer.depth.bevel, 0.045)), t / 2)
  return Math.max(t, 2 * halfHeight(t, b, k, D))
}

// ------------------------------------------------------------------------------------------------ plate
export interface PlateGeometryParams {
  shape: PlateShape
  cornerRadius: number
  thickness: number
  bevel: number
}

export function plateParams(shape: PlateShape, cornerRadius: number, plate: Plate): PlateGeometryParams {
  return { shape, cornerRadius, thickness: plate.thickness, bevel: plate.bevel }
}

export function plateGeometryKey(p: PlateGeometryParams): string {
  return `plate|${p.shape}|${r5(p.cornerRadius)}|${r5(p.thickness)}|${r5(p.bevel)}`
}

/** The plate body (worker scene._plate): centred on its mid-plane, placed at z = −thickness / 2 (front face at 0). */
export function buildPlateBody(p: PlateGeometryParams): Body {
  const outline = plateOutline(p.shape, p.cornerRadius)
  if (outline.length < 3) return { geometry: new THREE.BufferGeometry(), half: 0 }
  const th = Math.max(0, p.thickness)
  const b = Math.min(Math.max(0, p.bevel), th / 2)
  return bodyFromSplines([polySpline(outline)], th, b, 0, 8, 1)
}
