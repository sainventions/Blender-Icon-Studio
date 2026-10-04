// Depth controls of a layer (PLAN §11 Geometry): height-field bodies whose round edge has radius `bevel` ≤ thickness/2
// (both renderers clamp to exactly that: blender_worker scene._layer, viewport layerDepth). Thin parts and tips taper
// by themselves (z is limited by the inward distance), so no thin-feature limit applies any more: the inspector shows
// Roundness = bevel / (thickness / 2) and 1 is a full pill edge on every part wide enough to take it.
//
// Real-height stacking (PLAN §11 round 7, shared/presets.json "geometry" — the same rule as server bis.stacking): a
// layer's bodies are H = thickness + 2 · inflate · maxRadius · S tall (LayerGeometry.maxRadius, S = canvas.art.scale ×
// layer.transform.scale) and layers stack bottom → top: z0 = stackLift, z(i+1) = z(i) + H(i) + gap (gap = stackGap).
import type { Layer } from '../../../types'

/** presets.json "geometry": where the stack starts and the clearance between neighbouring layers' bodies. */
export interface StackRules {
  stackLift: number
  stackGap: number
}
/** server bis.stacking.DEFAULT_RULES — used when presets.json has no "geometry" section. */
export const DEFAULT_STACK_RULES: StackRules = { stackLift: 0, stackGap: 0.03 }
/** Two z values closer than this are "the same" when a stack is recognised (server Z_TOL). */
export const Z_TOL = 2e-4
/** The pre-round-7 default stack z_i = i × 0.13 (legacy projects re-stack like the server converts them). */
export const LEGACY_STEP = 0.13

const round5 = (v: number) => Math.round(v * 1e5) / 1e5
const num = (v: unknown, d = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : d)

/** The stacking rules of presets.json "geometry" (non-negative finite numbers; defaults otherwise). */
export function stackRules(presets?: { geometry?: Partial<StackRules> | null } | null): StackRules {
  const g = presets?.geometry ?? {}
  const pick = (k: keyof StackRules) => {
    const v = g[k]
    return typeof v === 'number' && Number.isFinite(v) ? Math.max(0, v) : DEFAULT_STACK_RULES[k]
  }
  return { stackLift: pick('stackLift'), stackGap: pick('stackGap') }
}

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

/** The shared body-height rule: H = thickness + 2 · inflate · maxRadius · S (server stacking.body_height). */
export function ruleHeight(l: Layer, maxRadius: number | null | undefined, S: number): number {
  const t = Math.max(0, num(l.depth.thickness))
  const k = Math.min(1, Math.max(0, num(l.depth.inflate)))
  return t + 2 * k * Math.max(0, num(maxRadius)) * Math.max(0, num(S, 1))
}

/** World height of a layer's bodies (the editor composes ruleHeight with the viewport's measure of the worker). */
export type BodyHeight = (l: Layer) => number

/**
 * Layers (bottom → top) re-stacked at their real heights: z0 = stackLift, z(i+1) = z(i) + H(i) + gap (gap = the rules'
 * stackGap unless given). Locked layers keep their z but still take their room in the stack. Returns the same array
 * when nothing moves.
 */
export function restack(
  layers: Layer[],
  heightOf: BodyHeight = (l) => l.depth.thickness,
  rules: StackRules = DEFAULT_STACK_RULES,
  gap: number = rules.stackGap,
): Layer[] {
  const g = Math.max(0, num(gap, rules.stackGap))
  let z = Math.max(0, num(rules.stackLift))
  let changed = false
  const out = layers.map((l) => {
    const nz = round5(z)
    // a locked layer stays put; the next one starts above wherever it sits. The running z stays UNROUNDED (like the
    // server's stacking.stack_z): accumulating the rounded values drifted 1e-5 off the server's import stack (Photos)
    z = (l.locked ? Math.max(z, l.depth.z) : z) + Math.max(0, heightOf(l)) + g
    if (l.locked || Math.abs(l.depth.z - nz) < 1e-9) return l
    changed = true
    return { ...l, depth: { ...l.depth, z: nz } }
  })
  return changed ? out : layers
}

/**
 * server bis.stacking.stack_gap: the gap of a real-height stack (z0 = stackLift and ONE clearance between every pair of
 * neighbours), the rules' stackGap for the pre-round-7 default stack (z_i = i × 0.13) and for a single layer at stackLift
 * — null for a custom (hand-placed) stack.
 */
export function stackGapOf(layers: Layer[], heightOf: BodyHeight, rules: StackRules = DEFAULT_STACK_RULES): number | null {
  if (!layers.length) return null
  const zs = layers.map((l) => num(l.depth.z))
  if (zs.length > 1 && zs.every((z, i) => Math.abs(z - i * LEGACY_STEP) <= Z_TOL)) return rules.stackGap
  if (Math.abs(zs[0] - rules.stackLift) > Z_TOL) return null
  if (zs.length < 2) return rules.stackGap
  const gaps = zs.slice(1).map((z, i) => z - (zs[i] + heightOf(layers[i])))
  const lo = Math.min(...gaps)
  const hi = Math.max(...gaps)
  if (hi - lo > 2 * Z_TOL || lo < -Z_TOL) return null
  return round5(Math.max(0, gaps.reduce((a, b) => a + b, 0) / gaps.length))
}

/**
 * Keep a real-height stack across an edit (the server re-stacks on structural edits; a PUT stores the project as sent,
 * so client-side edits re-stack here): when the layers BEFORE the edit formed a recognised stack (stackGapOf), the
 * edited layers are re-stacked with the same gap — a thicker / more inflated / rescaled / reordered / deleted layer
 * moves the ones above it. Custom stacks are left alone. Returns `after` itself when nothing moves.
 */
export function keepStack(
  before: Layer[],
  after: Layer[],
  heightBefore: BodyHeight,
  heightAfter: BodyHeight,
  rules: StackRules = DEFAULT_STACK_RULES,
): Layer[] {
  const gap = stackGapOf(before, heightBefore, rules)
  if (gap === null || !after.length) return after
  return restack(after, heightAfter, rules, gap)
}

/** True when an edit can change the stack heights / order (layer set, order, thickness, inflate, bevel, mode, scale). */
export function stackAffected(before: Layer[], after: Layer[], artScaleBefore: number, artScaleAfter: number): boolean {
  if (artScaleBefore !== artScaleAfter && after.length) return true
  if (before === after) return false
  if (before.length !== after.length) return true
  for (let i = 0; i < after.length; i++) {
    const a = before[i]
    const b = after[i]
    if (a === b) continue
    if (a.id !== b.id) return true
    const da = a.depth
    const db = b.depth
    if (
      da.thickness !== db.thickness ||
      da.inflate !== db.inflate ||
      da.bevel !== db.bevel ||
      a.transform.scale !== b.transform.scale ||
      a.mode !== b.mode
    )
      return true
  }
  return false
}
