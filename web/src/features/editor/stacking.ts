// Real-height layer stacking in the editor (PLAN §11 round 7; shared/presets.json "geometry"; server bis.stacking):
// body heights H = thickness + 2 · inflate · maxRadius · S from the geometry bundle, the Re-stack button, and keeping a
// recognised stack across client-side edits (thickness / inflate / scale / order / delete — a PUT stores the project as
// sent, so the server cannot re-stack those).
import type { GeometryBundle, Presets, Project } from '../../types'
import { useAppStore } from '../../store/app'
import { setCommitTransform } from '../../store/editor'
import { keepStack, restack, ruleHeight, stackAffected, stackRules, type BodyHeight } from './inspector/depth'
import { layerBodyHeight, layerScale } from './viewportBridge'

/**
 * Body height of the layers of `p`: the shared rule with the bundle's maxRadius — raised to the viewport's mirror of the
 * worker's real bodies (layerBodyHeight) where that is taller: pieces of one layer that overlap stack inside it, and a
 * bundle without maxRadius is measured from the silhouette. Layers without geometry: the rule with maxRadius 0.
 */
export function bodyHeights(p: Project, geometry: GeometryBundle | null | undefined): BodyHeight {
  return (l) => {
    const S = layerScale(p.canvas.art.scale, l.transform.scale)
    const lg = geometry?.layers?.[l.id]
    const H = ruleHeight(l, lg?.maxRadius, S)
    return lg ? Math.max(H, layerBodyHeight(l, lg, S)) : H
  }
}

/** The Re-stack button: every layer at its real height, z0 = stackLift, one stackGap between neighbours. */
export function restackProject(p: Project, geometry: GeometryBundle | null | undefined, presets: Presets | null | undefined): Project {
  const layers = restack(p.layers, bodyHeights(p, geometry), stackRules(presets))
  return layers === p.layers ? p : { ...p, layers }
}

/** After a client-side edit `before` → `after`: a recognised real-height stack stays one (keepStack); else `after`. */
export function keepProjectStack(
  before: Project,
  after: Project,
  geometry: GeometryBundle | null | undefined,
  presets: Presets | null | undefined,
): Project {
  if (before === after || !stackAffected(before.layers, after.layers, before.canvas.art.scale, after.canvas.art.scale)) return after
  const layers = keepStack(before.layers, after.layers, bodyHeights(before, geometry), bodyHeights(after, geometry), stackRules(presets))
  return layers === after.layers ? after : { ...after, layers }
}

/** Install keepProjectStack on every editor commit (EditorPage, at module load: before any edit can happen). */
export function installStackKeeper(): void {
  setCommitTransform((before, after, geometry) => keepProjectStack(before, after, geometry, useAppStore.getState().presets.data))
}
