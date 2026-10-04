// Depth controls of a layer (PLAN §11 Geometry): height-field bodies whose round edge has radius `bevel` ≤ thickness/2
// (both renderers clamp to exactly that: blender_worker scene._layer, viewport layerDepth). Thin parts and tips taper
// by themselves (z is limited by the inward distance), so no thin-feature limit applies any more: the inspector shows
// Roundness = bevel / (thickness / 2) and 1 is a near-pill edge on every part wide enough to take it (round 8: the body
// keeps a minimum vertical wall of 15 % of the half thickness, and thin parts are round tubes of their own width).
//
// Real-height stacking (PLAN §11 rounds 7 + 8, shared/presets.json "geometry" — the same rule as server bis.stacking):
// a layer's bodies are H = max(thickness + 2 · inflate · maxRadius · S, in-layer stacked height) tall
// (LayerGeometry.maxRadius, S = canvas.art.scale × layer.transform.scale) and layers stack bottom → top OVERLAP-AWARE:
// a layer only stacks above the lower layers whose footprints it overlaps in XY (features/editor/overlap.ts),
// z(i) = max(stackLift, max over those j of z(j) + H(j) + gap) (gap = stackGap); layers side by side share the base.
// HIDDEN layers take no stack slot (QA r11 N12, server stacking.stack_lower): j ranges over the visible lower layers
// only — nothing floats over a hidden layer — while a hidden layer's own z is still stacked over the visible layers it
// overlaps, so unhiding puts it there (the stack keeper then lifts the layers above it that it overlaps).
import type { Layer, SvgElement } from '../../../types'

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

/** Roundness 0..1 = bevel / bevelLimit (1 = near-pill edge; with Inflate a lens / sphere). */
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
 * For every layer i the indices of the LOWER layers j < i whose footprints it overlaps in XY (server
 * bis.stacking.overlap_lists; features/editor/overlap.ts computes them from the geometry bundle). Without them every
 * layer overlaps every lower one — the round-7 sequential stack.
 */
export type LowerLists = number[][]

/** Every layer overlaps every lower one (footprints unknown): the sequential stack. */
export function allLower(n: number): LowerLists {
  return Array.from({ length: n }, (_, i) => Array.from({ length: i }, (_, j) => j))
}

/** A layer takes a stack slot unless it is hidden (server stacking.is_visible). */
export const isShown = (l: Pick<Layer, 'visible'>): boolean => l.visible !== false

/**
 * The overlap lists the stack uses (QA r11 N12, server stacking.stack_lower): only the VISIBLE lower layers — no layer
 * stacks above a hidden one. Every layer keeps its list (a hidden layer's own z is stacked over the visible layers it
 * overlaps). Returns `lower` itself when no layer is hidden.
 */
export function stackLower(layers: Pick<Layer, 'visible'>[], lower: LowerLists): LowerLists {
  if (layers.every(isShown)) return lower
  return lower.map((low) => (low ?? []).filter((j) => j < layers.length && isShown(layers[j])))
}

/**
 * Layers (bottom → top) re-stacked at their real heights, OVERLAP-AWARE (PLAN §11 round 8, server stacking.restack):
 * z(i) = max(stackLift, max over the lower layers j it overlaps of z(j) + H(j) + gap) — layers side by side share the
 * base (gap = the rules' stackGap unless given). Hidden layers take no slot (stackLower). Locked layers keep their z
 * (the ones above still clear them). The running z stays UNROUNDED like the server's (the output is rounded to 5
 * decimals). Returns the same array when nothing moves.
 */
export function restack(
  layers: Layer[],
  heightOf: BodyHeight = (l) => l.depth.thickness,
  rules: StackRules = DEFAULT_STACK_RULES,
  gap: number = rules.stackGap,
  lower: LowerLists = allLower(layers.length),
): Layer[] {
  const g = Math.max(0, num(gap, rules.stackGap))
  const lift = Math.max(0, num(rules.stackLift))
  const low = stackLower(layers, lower)
  const z: number[] = []
  const h: number[] = []
  let changed = false
  const out = layers.map((l, i) => {
    h.push(Math.max(0, heightOf(l)))
    let zi = lift
    for (const j of low[i] ?? []) if (j < i) zi = Math.max(zi, z[j] + h[j] + g)
    if (l.locked) zi = num(l.depth.z)
    z.push(zi)
    const nz = round5(zi)
    if (l.locked || Math.abs(l.depth.z - nz) < 1e-9) return l
    changed = true
    return { ...l, depth: { ...l.depth, z: nz } }
  })
  return changed ? out : layers
}

/** For every layer: z(i) − max over its overlapped lower layers j of (z(j) + H(j)), null for a layer on the base
 *  (server stacking.stack_clearances; pass stackLower(layers, lower) for the stack's lists). */
export function stackClearances(zs: number[], hs: number[], lower: LowerLists): (number | null)[] {
  return zs.map((z, i) => {
    const low = (lower[i] ?? []).filter((j) => j < i)
    return low.length ? z - Math.max(...low.map((j) => zs[j] + hs[j])) : null
  })
}

/**
 * Server bis.stacking.stack_gap: the gap of a RULE stack, null for a custom (hand-placed) one. Rule stacks: the
 * overlap-aware real-height stack (round 8: base layers at stackLift, every other layer one gap above its overlapped
 * lower layers) and the round-7 sequential real-height stack (one gap between every pair of neighbours) — both give
 * their gap — and the pre-round-7 default stack (z_i = i × 0.13) and a stack with no overlapping layers at all (every
 * layer at stackLift) — both give the rules' stackGap. The overlap-aware stack is the current one (hidden layers take no
 * slot: stackLower) or the round-9 one, where hidden layers kept their slot (projects saved before QA r11 N12): an edit
 * then converts it.
 */
export function stackGapOf(
  layers: Layer[],
  heightOf: BodyHeight,
  rules: StackRules = DEFAULT_STACK_RULES,
  lower: LowerLists = allLower(layers.length),
): number | null {
  if (!layers.length) return null
  const zs = layers.map((l) => num(l.depth.z))
  if (zs.length > 1 && zs.every((z, i) => Math.abs(z - i * LEGACY_STEP) <= Z_TOL)) return rules.stackGap
  if (Math.abs(zs[0] - rules.stackLift) > Z_TOL) return null
  const hs = layers.map((l) => heightOf(l))
  const spread = (v: number[]) => Math.max(...v) - Math.min(...v)
  const mean = (v: number[]) => round5(Math.max(0, v.reduce((a, b) => a + b, 0) / v.length))
  const shown = stackLower(layers, lower)
  // the current rule, then round 9 (hidden layers kept their slot)
  for (const low of shown === lower ? [lower] : [shown, lower]) {
    const cl = stackClearances(zs, hs, low)
    const gaps = cl.filter((g): g is number => g !== null)
    if (!cl.every((g, i) => g !== null || Math.abs(zs[i] - rules.stackLift) <= Z_TOL)) continue
    if (!gaps.length) return rules.stackGap
    if (spread(gaps) <= 2 * Z_TOL && Math.min(...gaps) >= -Z_TOL) return mean(gaps)
  }
  // the round-7 sequential stack (one gap between every pair of neighbours)
  if (zs.length < 2) return rules.stackGap
  const seq = zs.slice(1).map((z, i) => z - (zs[i] + hs[i]))
  if (spread(seq) > 2 * Z_TOL || Math.min(...seq) < -Z_TOL) return null
  return mean(seq)
}

/**
 * Pairs of layers [j, i] (j < i) whose bodies cut into each other (server stacking.interpenetrations): footprints that
 * touch or overlap in XY (`touching`: overlap lists at a ~0 clearance) and z ranges [z, z + H] that overlap by more than
 * Z_TOL → [j, i, overlap depth]. Hidden layers are not rendered and take no stack slot (QA r11 N12): they never collide.
 */
export function interpenetrations(layers: Layer[], heightOf: BodyHeight, touching: LowerLists): [number, number, number][] {
  const zs = layers.map((l) => num(l.depth.z))
  const hs = layers.map((l) => heightOf(l))
  const out: [number, number, number][] = []
  stackLower(layers, touching).forEach((low, i) => {
    if (!isShown(layers[i])) return
    for (const j of low) {
      if (j >= i) continue
      const d = Math.min(zs[i] + hs[i], zs[j] + hs[j]) - Math.max(zs[i], zs[j])
      if (d > Z_TOL) out.push([j, i, d])
    }
  })
  return out
}

/**
 * Keep a real-height stack across an edit (the server re-stacks on structural edits; a PUT stores the project as sent,
 * so client-side edits re-stack here): when the layers BEFORE the edit formed a recognised rule stack (stackGapOf), the
 * edited layers are re-stacked overlap-aware with the same gap — a thicker / more inflated / rescaled / moved /
 * reordered / deleted / unhidden layer moves the layers above it that it overlaps (hiding one lets them down: a hidden
 * layer takes no slot). Custom stacks are left alone (the Depth section offers Re-stack when their bodies collide).
 * Returns `after` itself when nothing moves.
 */
export function keepStack(
  before: Layer[],
  after: Layer[],
  heightBefore: BodyHeight,
  heightAfter: BodyHeight,
  rules: StackRules = DEFAULT_STACK_RULES,
  lowerBefore: LowerLists = allLower(before.length),
  lowerAfter: LowerLists = allLower(after.length),
): Layer[] {
  const gap = stackGapOf(before, heightBefore, rules, lowerBefore)
  if (gap === null || !after.length) return after
  return restack(after, heightAfter, rules, gap, lowerAfter)
}

type ArtLike = number | { scale: number; x?: number; y?: number }

/** True when an edit can change the stack (layer set, order, visibility, thickness, inflate, bevel, mode, position /
 *  scale, art). */
export function stackAffected(before: Layer[], after: Layer[], artBefore: ArtLike, artAfter: ArtLike): boolean {
  const art = (a: ArtLike) => (typeof a === 'number' ? [a, 0, 0] : [a.scale, a.x ?? 0, a.y ?? 0])
  const [sb, xb, yb] = art(artBefore)
  const [sa, xa, ya] = art(artAfter)
  if ((sb !== sa || xb !== xa || yb !== ya) && after.length) return true
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
      isShown(a) !== isShown(b) ||
      da.thickness !== db.thickness ||
      da.inflate !== db.inflate ||
      da.bevel !== db.bevel ||
      a.transform.scale !== b.transform.scale ||
      a.transform.x !== b.transform.x ||
      a.transform.y !== b.transform.y ||
      a.mode !== b.mode
    )
      return true
  }
  return false
}

/**
 * What a layer of raster images is (PLAN §11 round 9, server stacking.card_elements / is_card_layer — the same softness
 * data: Element.softAlpha, measured from image.alphaSoftness at import): 'card' when every element is a SOFT-alpha
 * raster (a glow / shine / shadow; softAlpha true, or not measured yet — a project imported before round 9) — a flat
 * card, thin and without a dome; 'body' when every element is a raster but not all are soft (a crisp alpha silhouette:
 * iMessage's bubble, Vanced Neon's logo) — a real body like vector art; null when the layer holds vector art (or nothing).
 */
export function rasterLayerKind(
  elements: Pick<SvgElement, 'id' | 'kind' | 'softAlpha'>[],
  layer: Pick<Layer, 'elementIds'>,
): 'card' | 'body' | null {
  if (!layer.elementIds.length) return null
  const byId = new Map(elements.map((e) => [e.id, e]))
  const els = layer.elementIds.map((id) => byId.get(id))
  if (!els.every((e) => e?.kind === 'image')) return null
  return els.every((e) => e?.softAlpha !== false) ? 'card' : 'body'
}
