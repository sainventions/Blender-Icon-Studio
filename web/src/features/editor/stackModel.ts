// Real-height, overlap-aware layer stacking of a project (PLAN §11 rounds 7 + 8; shared/presets.json "geometry"; server
// bis.stacking). Pure (no store, no React): the node tests run it against fixtures exported from the server.
// Body heights H = max(thickness + 2 · inflate · maxRadius · S, in-layer stacked height) from the geometry bundle, XY
// footprints from its silhouettes (./overlap.ts), the Re-stack button, keeping a recognised stack across client-side
// edits, and the collisions of a hand-placed stack (the Depth section's Re-stack hint). ./stacking.ts installs it.
import type { GeometryBundle, Presets, Project } from '../../types'
import { layerBodyHeight, layerScale } from '../../viewport/geometry/layerGeometry'
import {
  interpenetrations,
  keepStack,
  restack,
  ruleHeight,
  stackAffected,
  stackGapOf,
  stackRules,
  type BodyHeight,
  type LowerLists,
} from './inspector/depth'
import { layerPlacements, overlapLists } from './overlap'

type Geo = Pick<GeometryBundle, 'layers'> | null | undefined

/** Footprints closer than this TOUCH (canvas units): the collision test of a hand-placed stack. The server tests
 *  touching at 1e-9 on footprints simplified by 0.002; exact outlines that share an edge come out ~1e-6 apart. */
export const TOUCH_CLEARANCE = 1e-4

type StackProject = Pick<Project, 'layers' | 'canvas'>

/**
 * Body height of the layers of `p` (worker scene._body_height = server stacking.layer_height): the shared rule with the
 * bundle's maxRadius, raised to the in-layer stacked height of overlapping pieces (layerBodyHeight, the viewport's
 * mirror of the worker's bodies) where that is taller. Layers without geometry: the rule with maxRadius 0.
 */
export function bodyHeights(p: StackProject, geometry: Geo): BodyHeight {
  return (l) => {
    const S = layerScale(p.canvas.art.scale, l.transform.scale)
    const lg = geometry?.layers?.[l.id]
    const H = ruleHeight(l, lg?.maxRadius, S)
    return lg ? Math.max(H, layerBodyHeight(l, lg, S)) : H
  }
}

/** The lower layers every layer of `p` overlaps in XY: footprints closer than `clearance` (server overlap_lists). */
export function lowerLists(p: StackProject, geometry: Geo, clearance: number): LowerLists {
  return overlapLists(layerPlacements(p.layers, geometry, p.canvas.art), clearance)
}

/** The Re-stack button: every layer at its real height, overlap-aware: z = stackLift, or one stackGap above the
 *  highest lower layer it overlaps in XY. */
export function restackProject<P extends StackProject>(p: P, geometry: Geo, presets: Presets | null | undefined): P {
  // no geometry bundle yet (the project is still loading): no heights / footprints to stack by, so leave it as it is
  if (!geometry?.layers) return p
  const rules = stackRules(presets)
  const layers = restack(p.layers, bodyHeights(p, geometry), rules, rules.stackGap, lowerLists(p, geometry, rules.stackGap))
  return layers === p.layers ? p : { ...p, layers }
}

/** After a client-side edit `before` → `after`: a recognised real-height stack stays one (keepStack); else `after`. */
export function keepProjectStack<P extends StackProject>(before: P, after: P, geometry: Geo, presets: Presets | null | undefined): P {
  if (before === after || !stackAffected(before.layers, after.layers, before.canvas.art, after.canvas.art)) return after
  // without the bundle the dome heights are unknown (H would be the bare thickness): never re-stack on a guess
  if (!geometry?.layers) return after
  const rules = stackRules(presets)
  const layers = keepStack(
    before.layers,
    after.layers,
    bodyHeights(before, geometry),
    bodyHeights(after, geometry),
    rules,
    lowerLists(before, geometry, rules.stackGap),
    lowerLists(after, geometry, rules.stackGap),
  )
  return layers === after.layers ? after : { ...after, layers }
}

export interface StackStatus {
  /** The gap of a recognised rule stack (kept through edits), null for a hand-placed stack. */
  gap: number | null
  /** Layers whose bodies cut into each other: [lower index, upper index, overlap depth] (server interpenetrations). */
  collisions: [number, number, number][]
}

/** Is `p` a rule stack, and which bodies collide (footprints touching in XY, z ranges overlapping)? */
export function stackStatus(p: StackProject, geometry: Geo, presets: Presets | null | undefined): StackStatus {
  const rules = stackRules(presets)
  const H = bodyHeights(p, geometry)
  // collisions only between layers whose footprints AND heights are known: while the bundle loads (or misses a layer
  // just added) an unknown footprint "overlaps everything" and its height is the bare thickness, so side-by-side layers
  // on the base would flash a false "Collide · Re-stack" warning (18 of the 68 corpus imports)
  const known = (i: number) => !!geometry?.layers?.[p.layers[i].id]
  const touching = lowerLists(p, geometry, TOUCH_CLEARANCE).map((low, i) => (known(i) ? low.filter(known) : []))
  return {
    gap: stackGapOf(p.layers, H, rules, lowerLists(p, geometry, rules.stackGap)),
    collisions: interpenetrations(p.layers, H, touching),
  }
}
