// Layer / plate geometry: splines → pill meshes, memoised by LayerGeometry.hash + depth params and disposed when
// no mounted component uses them any more.
//
// Units mirror the worker (scene.py `_layer`): a layer's bodies live in ART-LOCAL units under one uniform scale
// S = canvas.art.scale × layer.transform.scale, so the extrusion is built with thickness / S and bevel / S and the
// world thickness and round-bevel radius equal the layer's depth values exactly (a scaled layer keeps a circular
// bevel; `safeRadius` is in art units, so the clamp happens in local units too).
import * as THREE from 'three'
import type { Layer, LayerGeometry, Plate, PlateShape, Spline } from '../../types'
import { plateOutline } from '../../lib/shapes'
import { RefCache } from '../refCache'
import { buildPillGeometry } from './pillGeometry'
import { polygonToGroup, splinesToGroups } from './flatten'
import { MIN_BEVEL, prepareSplines } from './fillet'

export const geometryCache = new RefCache<string, THREE.BufferGeometry>((g) => g.dispose())

/** Smallest layer scale (the worker clamps the same way; a zero scale would make the normal matrix singular). */
export const MIN_LAYER_SCALE = 1e-4

export interface DepthParams {
  /** Local (art-unit) thickness = world thickness / scale. */
  thickness: number
  /** Local round-bevel radius after the worker's clamp. */
  bevel: number
  segments: number
  /** Local → world uniform scale S. */
  scale: number
}

const r5 = (v: number) => Math.round(v * 1e5) / 1e5

/** S = canvas.art.scale × layer.transform.scale (clamped like the worker). */
export function layerScale(artScale: number, layerScaleValue: number): number {
  return Math.max(MIN_LAYER_SCALE, (Number.isFinite(artScale) ? artScale : 1) * (Number.isFinite(layerScaleValue) ? layerScaleValue : 1))
}

/**
 * Worker `effective_bevel` (PLAN D3) in local units: bevel = min(requested, 0.9 × safeRadius, thickness / 2); requests
 * ≤ MIN_BEVEL extrude a sharp slab. The total thickness is always the layer's thickness.
 */
export function effectiveDepth(layer: Layer, lg: LayerGeometry | null, scale: number): DepthParams {
  const S = Math.max(MIN_LAYER_SCALE, scale)
  const thickness = Math.max(1e-4, Math.max(0, layer.depth.thickness) / S)
  const requested = Math.max(0, layer.depth.bevel) / S
  const safe = lg && lg.safeRadius > 0 ? lg.safeRadius : Infinity
  const bevel = requested <= MIN_BEVEL ? 0 : Math.min(requested, 0.9 * safe, thickness / 2)
  return {
    thickness,
    bevel,
    segments: Math.max(1, Math.min(16, Math.round(layer.depth.bevelSegments || 6))),
    scale: S,
  }
}

function depthKey(d: DepthParams): string {
  return `T${r5(d.thickness)}B${r5(d.bevel)}S${d.segments}`
}

function buildFromSplines(splines: Spline[], d: DepthParams): THREE.BufferGeometry {
  try {
    const groups = splinesToGroups(prepareSplines(splines, d.bevel))
    return buildPillGeometry(groups, { thickness: d.thickness, bevel: d.bevel, segments: d.segments })
  } catch (err) {
    console.warn('[viewport] geometry build failed', err)
    return new THREE.BufferGeometry()
  }
}

export interface BodyPart {
  key: string
  /** Region element id (individual mode) or null for the silhouette body. */
  elementId: string | null
  opacity: number
  /** World z offset of the piece (Region.zSub; values ≥ 1 are sub-layer indices × 0.001, like the worker). */
  zSub: number
  /** Raster-image region painted from the layer texture: honour the texture's alpha (worker: image alpha). */
  alpha: boolean
  /** Index of the region in LayerGeometry.regions (individual pieces), null for the silhouette body. */
  regionIndex: number | null
  build: () => THREE.BufferGeometry
}

/** Worker REGION_DZ handling: zSub < 1 is a world offset, zSub ≥ 1 a sub-layer index (capped at 40). */
export function regionOffset(zSub: number | null | undefined): number {
  const z = Number.isFinite(zSub as number) ? (zSub as number) : 0
  return z >= 1 ? Math.min(z, 40) * 0.001 : z
}

/**
 * Worker scene.touching_opaque (round 4): some regions of the layer share edges (the union silhouette has fewer outer
 * contours than the regions together) and every region is opaque vector paint with no sub-layer offset — the layer then
 * renders as ONE body painted by the layer texture (bevelled one by one, every shared edge became a V-groove showing
 * the plate as a white sliver with a rim highlight).
 */
export function touchingOpaque(lg: LayerGeometry | null | undefined): boolean {
  const regions = lg?.regions ?? []
  const sil = lg?.silhouette ?? []
  if (regions.length < 2 || !sil.length) return false
  if ((lg?.images ?? []).length) return false
  if (regions.some((r) => (r.opacity ?? 1) < 0.999 || Number(r.zSub ?? 0) !== 0)) return false
  if (regions.some((r) => (r.paint.type === 'linear' || r.paint.type === 'radial') && r.paint.stops.some((s) => (s.opacity ?? 1) < 0.999)))
    return false
  const silOuter = sil.filter((s) => !s.hole).length
  const regOuter = regions.reduce((n, r) => n + (r.splines ?? []).filter((s) => !s.hole).length, 0)
  return silOuter > 0 && silOuter < regOuter
}

/** One body (silhouette) instead of one per region: layer mode 'combined' or touching opaque pieces (worker). */
export function isCombinedBody(layer: Layer, lg: LayerGeometry): boolean {
  return layer.mode === 'combined' || touchingOpaque(lg)
}

/** The meshes of one layer: one per region ('individual') or the union silhouette ('combined' / touching pieces). */
export function layerBodyParts(layer: Layer, lg: LayerGeometry, d: DepthParams): BodyPart[] {
  const dk = depthKey(d)
  const base = `${lg.layerId}:${lg.hash}`
  const silhouette = lg.silhouette ?? []
  const regions = lg.regions ?? []
  if (!silhouette.length && !regions.length) return [] // raster-only layer (cards)
  if (isCombinedBody(layer, lg) || regions.length === 0) {
    const splines = silhouette.length ? silhouette : regions.flatMap((r) => r.splines)
    return [
      {
        key: `${base}|sil|${dk}`,
        elementId: null,
        opacity: 1,
        zSub: 0,
        alpha: false,
        regionIndex: null,
        build: () => buildFromSplines(splines, d),
      },
    ]
  }
  const rasters = new Set((lg.images ?? []).map((im) => im?.elementId))
  const autoPaint = (layer.fill?.type ?? 'auto') === 'auto'
  return regions.map((region, i) => ({
    key: `${base}|r${i}:${region.elementId}|${dk}`,
    elementId: region.elementId,
    opacity: region.opacity ?? 1,
    zSub: regionOffset(region.zSub),
    alpha: autoPaint && rasters.has(region.elementId),
    regionIndex: i,
    build: () => buildFromSplines(region.splines ?? [], d),
  }))
}

export interface PlateGeometryParams {
  shape: PlateShape
  cornerRadius: number
  thickness: number
  bevel: number
}

export function plateGeometryKey(p: PlateGeometryParams): string {
  return `plate|${p.shape}|${r5(p.cornerRadius)}|${r5(p.thickness)}|${r5(p.bevel)}`
}

/** Plate occupies z ∈ [−thickness, 0] (front face at z = 0, PLAN §3); bevel = min(bevel, 0.9, thickness / 2). */
export function buildPlateGeometry(p: PlateGeometryParams): THREE.BufferGeometry {
  const group = polygonToGroup(plateOutline(p.shape, p.cornerRadius))
  if (!group) return new THREE.BufferGeometry()
  const T = Math.max(p.thickness, 1e-3)
  const bevel = Math.max(0, Math.min(p.bevel, T / 2, 0.9))
  return buildPillGeometry([group], { thickness: T, bevel, segments: 8, creaseDeg: 30, zOffset: -T })
}

export function plateParams(shape: PlateShape, cornerRadius: number, plate: Plate): PlateGeometryParams {
  return { shape, cornerRadius, thickness: plate.thickness, bevel: plate.bevel }
}
