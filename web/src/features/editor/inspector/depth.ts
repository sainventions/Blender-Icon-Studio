// Depth controls of a layer (PLAN §11 Geometry): height-field bodies whose round edge has radius `bevel` ≤ thickness/2
// (both renderers clamp to exactly that: blender_worker scene._layer, viewport layerDepth). Thin parts and tips taper
// by themselves (z is limited by the inward distance), so no thin-feature limit applies any more: the inspector shows
// Roundness = bevel / (thickness / 2) and 1 is a full pill edge on every part wide enough to take it.
import type { Layer } from '../../../types'

/** Gap left between stacked bodies by "Re-stack" (art units). */
export const STACK_GAP = 0.03
/** Re-stack never packs layers tighter than the import default (PLAN §2 default stack: z_i = i × 0.13). */
export const STACK_MIN_STEP = 0.13

const round5 = (v: number) => Math.round(v * 1e5) / 1e5

/** Largest round-edge radius a layer takes: half its thickness (the renderers' own clamp). */
export function bevelLimit(l: Layer): number {
  return Math.max(0, l.depth.thickness / 2)
}

/** Roundness 0..1 = bevel / bevelLimit (1 = full pill edge; with Inflate a lens / sphere). */
export function roundnessOf(l: Layer): number {
  const lim = bevelLimit(l)
  return lim > 1e-9 ? Math.min(1, Math.max(0, l.depth.bevel / lim)) : 0
}

/** The layer with Roundness `r` (bevel = r × limit). */
export function withRoundness(l: Layer, r: number): Layer {
  const bevel = round5(Math.min(1, Math.max(0, r)) * bevelLimit(l))
  return bevel === l.depth.bevel ? l : { ...l, depth: { ...l.depth, bevel } }
}

/** The layer with a new thickness, keeping its Roundness (the bevel scales with the new limit). */
export function withThickness(l: Layer, thickness: number): Layer {
  if (thickness === l.depth.thickness) return l
  const r = roundnessOf(l)
  return withRoundness({ ...l, depth: { ...l.depth, thickness } }, r)
}

/** World height of a layer's bodies; without a measure (no geometry yet) its thickness. The editor passes the
 *  viewport's mirror of blender_worker scene._body_height (inflated domes use the silhouette's inradius). */
export type BodyHeight = (l: Layer) => number

/** Layers (bottom → top) re-stacked from z = 0 so no body overlaps the next one; locked layers keep their z but still
 *  take their room in the stack. Returns the same array when nothing moves. */
export function restack(layers: Layer[], heightOf: BodyHeight = (l) => l.depth.thickness): Layer[] {
  let z = 0
  let changed = false
  const out = layers.map((l) => {
    const nz = round5(z)
    // a locked layer stays put; the next one starts above wherever it sits
    z = (l.locked ? Math.max(nz, l.depth.z) : nz) + Math.max(STACK_MIN_STEP, heightOf(l) + STACK_GAP)
    if (l.locked || Math.abs(l.depth.z - nz) < 1e-9) return l
    changed = true
    return { ...l, depth: { ...l.depth, z: nz } }
  })
  return changed ? out : layers
}
