// Editor keyboard shortcuts. Ignored while typing in inputs or while a dialog is open.
import { useEffect } from 'react'
import { isTypingTarget } from '../../lib/hooks'
import { APPEARANCE_IDS } from '../../lib/projectOps'
import { useEditor } from '../../store/editor'
import { useRender } from '../../store/render'
import { useUi, type StageMode } from '../../store/ui'
import { animateExplode, duplicateProject } from './actions'
import { copyStyle, pasteStyle } from '../looks/styleActions'

export interface ShortcutDef {
  keys: string
  label: string
  group: 'General' | 'View' | 'Layers' | 'Render'
}

/** Shown in the shortcuts dialog. Keep in sync with the handler below. */
export const SHORTCUTS: ShortcutDef[] = [
  { keys: 'Mod+Z', label: 'Undo', group: 'General' },
  { keys: 'Mod+Shift+Z', label: 'Redo', group: 'General' },
  { keys: 'Mod+S', label: 'Save now', group: 'General' },
  { keys: 'Mod+D', label: 'Duplicate project', group: 'General' },
  { keys: 'Mod+Alt+C', label: 'Copy style (materials, depth, plate, lighting)', group: 'General' },
  { keys: 'Mod+Alt+V', label: 'Paste style onto this project', group: 'General' },
  { keys: 'E', label: 'Export…', group: 'General' },
  { keys: '?', label: 'Keyboard shortcuts', group: 'General' },
  { keys: '1 – 6', label: 'Switch appearance (Default, Dark, Clear Light, Clear Dark, Tinted Light, Tinted Dark)', group: 'View' },
  { keys: 'V', label: 'Cycle stage: Viewport → Render → Compare → Matrix', group: 'View' },
  { keys: 'O', label: 'Front / orbit view', group: 'View' },
  { keys: 'X', label: 'Explode layer stack', group: 'View' },
  { keys: 'G', label: 'Icon grid overlay', group: 'View' },
  { keys: 'Mod+\\', label: 'Toggle side panels', group: 'View' },
  { keys: 'Mod+0', label: 'Zoom to fit', group: 'View' },
  { keys: 'Mod+A', label: 'Select all layers', group: 'Layers' },
  { keys: 'Esc', label: 'Clear selection', group: 'Layers' },
  { keys: 'Mod+G', label: 'Merge selected layers', group: 'Layers' },
  { keys: 'Del', label: 'Delete selected layers', group: 'Layers' },
  { keys: 'Arrows', label: 'Nudge layer (Shift ×10, Alt fine)', group: 'Layers' },
  { keys: 'Mod+]', label: 'Bring layer forward', group: 'Layers' },
  { keys: 'Mod+[', label: 'Send layer backward', group: 'Layers' },
  { keys: 'H', label: 'Hide / show selected layers', group: 'Layers' },
  { keys: 'R', label: 'Render Cycles preview', group: 'Render' },
  { keys: 'Shift+R', label: 'Final render (1024 px, one-shot)', group: 'Render' },
  { keys: 'Shift+M', label: 'Render all six renditions', group: 'Render' },
]

const STAGE_ORDER: StageMode[] = ['viewport', 'render', 'compare', 'matrix']

export function moveSelectedLayer(dir: 1 | -1) {
  const ed = useEditor.getState()
  const p = ed.project
  const id = ed.selection.primary
  if (!p || !id) return
  const i = p.layers.findIndex((l) => l.id === id)
  const j = i + dir
  if (i < 0 || j < 0 || j >= p.layers.length) return
  ed.reorder(i, j)
}

export function useEditorShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isTypingTarget(e.target)) return
      if (document.querySelector('[data-dialog]')) return
      const mod = e.ctrlKey || e.metaKey
      const key = e.key
      const lower = key.toLowerCase()
      const ed = useEditor.getState()
      const ui = useUi.getState()

      const handled = () => {
        e.preventDefault()
        e.stopPropagation()
      }

      if (mod && e.altKey) {
        // e.code: Ctrl+Alt is AltGr on many layouts, so e.key may be a different character.
        if (e.code === 'KeyC') return handled(), void copyStyle()
        if (e.code === 'KeyV') return handled(), void pasteStyle()
        return
      }
      if (mod) {
        if (lower === 'z' && !e.shiftKey) return handled(), ed.undo()
        if ((lower === 'z' && e.shiftKey) || lower === 'y') return handled(), ed.redo()
        if (lower === 's') return handled(), void ed.flushSave()
        if (lower === 'd') return handled(), void duplicateProject()
        if (lower === 'g') return handled(), void ed.mergeLayers()
        if (lower === 'a') return handled(), ed.selectLayers(ed.project?.layers.map((l) => l.id) ?? [])
        if (key === '\\') {
          handled()
          const hide = !(ui.leftCollapsed && ui.rightCollapsed)
          return ui.set({ leftCollapsed: hide, rightCollapsed: hide })
        }
        if (key === ']') return handled(), moveSelectedLayer(1)
        if (key === '[') return handled(), moveSelectedLayer(-1)
        if (key === '0') return ui.stageMode === 'matrix' ? undefined : (handled(), ui.set({ zoom: 1 }))
        return
      }
      if (e.altKey && !key.startsWith('Arrow')) return

      if (/^[1-6]$/.test(key)) return handled(), ed.setAppearance(APPEARANCE_IDS[Number(key) - 1])
      if (key.startsWith('Arrow')) {
        if (!ed.selection.layerIds.length) return
        handled()
        const d = e.shiftKey ? 0.1 : e.altKey ? 0.001 : 0.01
        const [dx, dy] = key === 'ArrowLeft' ? [-d, 0] : key === 'ArrowRight' ? [d, 0] : key === 'ArrowUp' ? [0, d] : [0, -d]
        return ed.nudge(dx, dy)
      }
      switch (lower) {
        case 'r':
          handled()
          return void useRender.getState().render(e.shiftKey ? 'final' : 'preview')
        case 'm':
          if (e.shiftKey) return handled(), void useRender.getState().renderRenditions(ui.matrixQuality)
          return
        case 'e':
          return handled(), ui.openDialog('export')
        case 'g':
          if (ui.stageMode === 'matrix') return // no grid in the matrix (finished renders only)
          return handled(), ui.set({ showGrid: !ui.showGrid })
        case 'x':
          if (ui.stageMode === 'matrix') return
          return handled(), animateExplode()
        case 'o':
          return handled(), ui.set({ view3d: ui.view3d === 'front' ? 'orbit' : 'front' })
        case 'v': {
          handled()
          const i = STAGE_ORDER.indexOf(ui.stageMode)
          return ui.set({ stageMode: STAGE_ORDER[(i + (e.shiftKey ? STAGE_ORDER.length - 1 : 1)) % STAGE_ORDER.length] })
        }
        case 'h': {
          const p = ed.project
          if (!p || !ed.selection.layerIds.length) return
          handled()
          const sel = new Set(ed.selection.layerIds.filter((id) => !p.layers.find((l) => l.id === id)?.locked))
          if (!sel.size) return
          const anyVisible = p.layers.some((l) => sel.has(l.id) && l.visible)
          return ed.commit((proj) => ({ ...proj, layers: proj.layers.map((l) => (sel.has(l.id) ? { ...l, visible: !anyVisible } : l)) }))
        }
        case 'escape':
          if (ed.selection.layerIds.length || ed.selection.elementIds.length) return handled(), ed.clearSelection()
          return
        case 'delete':
        case 'backspace':
          if (!ed.selection.layerIds.length) return
          return handled(), ed.deleteLayers()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
