// Real-height, overlap-aware layer stacking in the editor (PLAN §11 rounds 7 + 8; the model is ./stackModel.ts): keeps a
// recognised stack across client-side edits (thickness / inflate / position / scale / order / delete — a PUT stores the
// project as sent, so the server cannot re-stack those) by installing keepProjectStack as the store's commit transform,
// and reports the collisions of a hand-placed stack (the Re-stack hints of the Depth section and the Layers panel).
import { useMemo } from 'react'
import { useAppStore } from '../../store/app'
import { setCommitTransform, useEditor } from '../../store/editor'
import { keepProjectStack, restackProject, stackStatus, type StackStatus } from './stackModel'

export { bodyHeights, keepProjectStack, lowerLists, restackProject, stackStatus, type StackStatus } from './stackModel'

/** Install keepProjectStack on every editor commit (EditorPage, at module load: before any edit can happen). */
export function installStackKeeper(): void {
  setCommitTransform((before, after, geometry) => keepProjectStack(before, after, geometry, useAppStore.getState().presets.data))
}

/** The open project's stack: rule stack (gap) or hand-placed, and which layer bodies cut into each other. */
export function useStackStatus(): StackStatus | null {
  const project = useEditor((s) => s.project)
  const geometry = useEditor((s) => s.geometry)
  const presets = useAppStore((s) => s.presets.data)
  const layers = project?.layers
  const canvas = project?.canvas
  return useMemo(() => (layers && canvas ? stackStatus({ layers, canvas }, geometry, presets) : null), [layers, canvas, geometry, presets])
}

/** Re-stack the open project (one undo step): every layer at its real height, overlap-aware. */
export function restackOpenProject(): void {
  const ed = useEditor.getState()
  const presets = useAppStore.getState().presets.data
  ed.commit((p) => restackProject(p, ed.geometry, presets), { coalesce: 'restack' })
}

/** "“A” and “B” cut 0.11 into each other" (+ how many more pairs) for the collision hints. */
export function collisionText(status: StackStatus, names: string[]): string {
  const [j, i, d] = status.collisions[0]
  const more = status.collisions.length - 1
  const q = (k: number) => `“${names[k] ?? `Layer ${k + 1}`}”`
  return `${q(j)} and ${q(i)} cut ${d.toFixed(2)} into each other${more > 0 ? ` (+${more} more pair${more > 1 ? 's' : ''})` : ''}`
}
