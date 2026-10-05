// Looks / copy-paste style actions for the open project (top bar, Document tab, shortcuts).
import { useSyncExternalStore } from 'react'
import { errorMessage, projectsApi } from '../../api'
import { readCopiedStyle, sanitizeStyle, writeCopiedStyle } from '../../lib/looks'
import { prettyShortcut } from '../../lib/format'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { toast } from '../../store/toasts'

/** Look currently being applied (cards show a spinner on it). */
let applyingLook: string | null = null
const listeners = new Set<() => void>()
export function getApplyingLook() {
  return applyingLook
}
export function subscribeApplyingLook(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}
export function useApplyingLook(): string | null {
  return useSyncExternalStore(subscribeApplyingLook, getApplyingLook, () => null)
}
function setApplying(id: string | null) {
  applyingLook = id
  listeners.forEach((l) => l())
}

/** Toast "Undo" for a style change, only while that project is still open (the toast outlives navigation). */
const undoAction = (projectId: string | undefined) => ({
  label: 'Undo',
  onClick: () => {
    const ed = useEditor.getState()
    if (projectId && ed.project?.id === projectId) ed.undo()
  },
})

export async function applyLook(lookId: string) {
  const ed = useEditor.getState()
  if (!ed.project || ed.busy) return
  const look = useAppStore.getState().presets.data?.looks?.[lookId]
  const label = look?.label ?? lookId
  setApplying(lookId)
  try {
    const ok = await ed.applyStyle({ look: lookId }, `Apply look “${label}”`)
    if (ok) toast.success(`${label} look applied`, { id: 'style-applied', description: look?.description, action: undoAction(ed.project?.id) })
  } finally {
    setApplying(null)
  }
}

export async function copyStyle() {
  const ed = useEditor.getState()
  const p = ed.project
  if (!p) return
  if (!(await ed.flushSave())) {
    toast.error('Could not copy the style', { description: 'Pending changes could not be saved first.' })
    return
  }
  try {
    const style = sanitizeStyle(await projectsApi.getStyle(p.id))
    writeCopiedStyle({ style, sourceName: p.name, sourceId: p.id, copiedAt: Date.now() })
    if (!p.layers.length) {
      // The server falls back to default layer materials for a project without layers: say so, since pasting
      // this would reset every layer of the target to that default.
      toast.warning('Style copied: plate and lighting only', {
        id: 'style-copied',
        description: `“${p.name}” has no layers, so pasting gives every layer the default material.`,
      })
      return
    }
    toast.success('Style copied', {
      id: 'style-copied',
      description: `From “${p.name}”. Paste it onto any project with ${prettyShortcut('Mod+Alt+V')}.`,
    })
  } catch (e) {
    toast.error('Could not copy the style', { description: errorMessage(e) })
  }
}

export async function pasteStyle() {
  const ed = useEditor.getState()
  if (!ed.project || ed.busy) return
  const copied = readCopiedStyle()
  if (!copied) {
    toast.info('No style copied yet', { description: `Open the project whose look you like and press ${prettyShortcut('Mod+Alt+C')}.` })
    return
  }
  const ok = await ed.applyStyle({ style: copied.style }, 'Paste style')
  if (ok)
    toast.success('Style pasted', {
      id: 'style-applied',
      description: copied.sourceId === useEditor.getState().project?.id ? `Re-applied this project’s copied style.` : `From “${copied.sourceName}”.`,
      action: undoAction(useEditor.getState().project?.id),
    })
}
