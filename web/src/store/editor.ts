// The document store: the open project, undo/redo history, debounced autosave, geometry and selection.
//
// Edits go through `commit(fn, opts)` where `fn` returns a new Project (structural sharing via the helpers in
// lib/projectOps). Slider drags pass a `coalesce` key so a whole gesture becomes one undo step. Structural
// operations (re-split / merge / split / move elements) run on the server, which returns the new Project.
import { create } from 'zustand'
import type { AppearanceId, GeometryBundle, Project, SplitStrategy } from '../types'
import { errorMessage, projectsApi } from '../api'
import { removeLayers, reorderLayers, structuralSig, updateLayers } from '../lib/projectOps'
import { toast } from './toasts'
import { useAppStore } from './app'

export type SaveState = 'saved' | 'dirty' | 'saving' | 'error'
export type SelectMode = 'replace' | 'toggle' | 'range'

export interface Selection {
  layerIds: string[]
  primary: string | null
  elementIds: string[]
}

export interface CommitOptions {
  /** Merge consecutive commits with the same key (within 1 s) into one undo step. */
  coalesce?: string
  /** Record in undo history (default true). */
  history?: boolean
  /** Counts as a render-relevant edit (default true). */
  render?: boolean
}

interface EditorState {
  projectId: string | null
  project: Project | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: string | null

  geometry: GeometryBundle | null
  geometrySig: string | null
  geometryLoading: boolean
  geometryError: string | null

  past: Project[]
  future: Project[]
  rev: number
  savedRev: number
  saveState: SaveState
  saveError: string | null
  lastSavedAt: number | null
  /** Bumped by every render-relevant edit (drives auto renders). */
  editSeq: number

  selection: Selection
  /** Label of a running structural operation (disables structural UI). */
  busy: string | null

  open: (id: string) => Promise<void>
  close: () => void
  commit: (fn: (p: Project) => Project, opts?: CommitOptions) => void
  undo: () => void
  redo: () => void
  flushSave: () => Promise<boolean>
  refreshGeometry: (force?: boolean) => Promise<void>

  selectLayer: (id: string | null, mode?: SelectMode) => void
  selectLayers: (ids: string[]) => void
  selectElement: (id: string, mode?: SelectMode) => void
  clearSelection: () => void

  setAppearance: (a: AppearanceId) => void
  rename: (name: string) => void
  reorder: (fromIndex: number, toIndex: number) => void
  deleteLayers: (ids?: string[]) => void
  nudge: (dx: number, dy: number) => void

  resplit: (strategy: SplitStrategy) => Promise<void>
  mergeLayers: (ids?: string[]) => Promise<void>
  splitLayer: (id: string, mode: 'elements' | 'islands') => Promise<void>
  moveElements: (elementIds: string[], toLayerId: string | null) => Promise<void>
}

const HISTORY_LIMIT = 200
const COALESCE_MS = 1000
const SAVE_DEBOUNCE_MS = 500

let lastCommit: { key: string | null; at: number } = { key: null, at: 0 }

// Pointer gestures (slider drags, label scrubs, dial turns, gradient stops): all same-key commits between
// pointerdown and pointerup form ONE undo step however long the user pauses mid-drag, and releasing ends the
// step (the next drag is a new one). Outside gestures (keyboard, typing) the 1 s COALESCE_MS window applies.
let gestureActive = false
if (typeof window !== 'undefined') {
  window.addEventListener('pointerdown', () => (gestureActive = true), true)
  const endGesture = () => {
    if (!gestureActive) return
    // Capture phase runs before the control's own pointerup handler (which commits the final value): defer.
    window.setTimeout(() => {
      gestureActive = false
      lastCommit = { key: null, at: 0 }
    }, 0)
  }
  window.addEventListener('pointerup', endGesture, true)
  window.addEventListener('pointercancel', endGesture, true)
  window.addEventListener('blur', endGesture)
}
let saveTimer: number | undefined
let retryTimer: number | undefined
let inflight: Promise<void> | null = null
let geoToken = 0
let openToken = 0

const emptySelection: Selection = { layerIds: [], primary: null, elementIds: [] }

/** Save a snapshot of a project that is no longer open (leaving the editor), after any in-flight PUT. */
async function saveDetached(snapshot: Project): Promise<void> {
  if (inflight) await inflight // never rejects (errors are handled inside flushSave)
  try {
    await projectsApi.save(snapshot)
    // The home screen may already have listed the project (name / updatedAt) from before this save.
    if (useAppStore.getState().projects.data) void useAppStore.getState().loadProjects()
  } catch (e) {
    toast.error(`Could not save “${snapshot.name}”`, { description: `${errorMessage(e)} — the last edits were not stored.` })
  }
}

function pruneSelection(sel: Selection, p: Project | null): Selection {
  if (!p) return emptySelection
  const ids = new Set(p.layers.map((l) => l.id))
  const layerIds = sel.layerIds.filter((id) => ids.has(id))
  const els = new Set(p.layers.flatMap((l) => l.elementIds))
  const elementIds = sel.elementIds.filter((id) => els.has(id))
  const primary = sel.primary && ids.has(sel.primary) ? sel.primary : (layerIds[layerIds.length - 1] ?? null)
  if (layerIds.length === sel.layerIds.length && elementIds.length === sel.elementIds.length && primary === sel.primary)
    return sel
  return { layerIds, primary, elementIds }
}

/**
 * Some server operations rewrite the element set itself: "split into islands" replaces a multi-island element
 * `e5` by `e5-1`, `e5-2`, … in the pipeline's element store (A's `_explode_islands`). History snapshots taken
 * before that still reference `e5`, which no longer exists server-side — undoing would silently drop the art
 * from the geometry. Returns a mapper that rewrites a snapshot onto the new element set (each removed id is
 * replaced by its pieces, in paint order), or null when the element set did not change.
 */
function elementRemapper(before: Project, after: Project): ((p: Project) => Project) | null {
  const afterIds = new Set(after.elements.map((e) => e.id))
  const beforeIds = new Set(before.elements.map((e) => e.id))
  const removed = before.elements.map((e) => e.id).filter((id) => !afterIds.has(id))
  const added = after.elements.map((e) => e.id).filter((id) => !beforeIds.has(id))
  if (!removed.length && !added.length) return null
  const replacement = new Map<string, string[]>(removed.map((id) => [id, added.filter((a) => a.startsWith(`${id}-`))]))
  return (p) => {
    let touched = false
    const layers = p.layers.map((l) => {
      if (!l.elementIds.some((id) => replacement.has(id))) return l
      touched = true
      const ids = l.elementIds.flatMap((id) => replacement.get(id) ?? [id])
      return { ...l, elementIds: [...new Set(ids)] }
    })
    return { ...p, elements: after.elements, layers: touched ? layers : p.layers }
  }
}

export const useEditor = create<EditorState>((set, get) => {
  const scheduleSave = (ms = SAVE_DEBOUNCE_MS) => {
    window.clearTimeout(saveTimer)
    saveTimer = window.setTimeout(() => void get().flushSave(), ms)
  }

  /** Run a server-side structural operation that returns the new project. */
  const structural = async (label: string, op: (id: string) => Promise<Project>, after?: (p: Project) => Partial<EditorState>) => {
    const s = get()
    if (!s.project || s.busy) return
    set({ busy: label })
    try {
      const ok = await get().flushSave()
      if (!ok) throw new Error('Could not save pending changes first.')
      const before = get().project!
      const next = await op(before.id)
      if (get().project?.id !== before.id) return
      // Keep the UI-only appearance the user is on (re-saved below if it differs from the server's copy).
      const merged: Project = { ...next, appearance: get().project!.appearance }
      const differs = merged.appearance !== next.appearance
      const remap = elementRemapper(before, merged)
      set((st) => ({
        project: merged,
        past: (remap ? [...st.past, before].map(remap) : [...st.past, before]).slice(-HISTORY_LIMIT),
        future: [],
        rev: st.rev + 1,
        savedRev: differs ? st.savedRev : st.rev + 1,
        saveState: differs ? 'dirty' : 'saved',
        editSeq: st.editSeq + 1,
        selection: pruneSelection(st.selection, merged),
        ...(after ? after(merged) : {}),
      }))
      lastCommit = { key: null, at: 0 }
      if (differs) scheduleSave(50)
      void get().refreshGeometry()
    } catch (e) {
      toast.error(`${label} failed`, { description: errorMessage(e) })
    } finally {
      set({ busy: null })
    }
  }

  return {
    projectId: null,
    project: null,
    status: 'idle',
    error: null,
    geometry: null,
    geometrySig: null,
    geometryLoading: false,
    geometryError: null,
    past: [],
    future: [],
    rev: 0,
    savedRev: 0,
    saveState: 'saved',
    saveError: null,
    lastSavedAt: null,
    editSeq: 0,
    selection: emptySelection,
    busy: null,

    open: async (id) => {
      const token = ++openToken
      if (get().project && get().project!.id !== id) await get().flushSave()
      window.clearTimeout(saveTimer)
      geoToken++
      lastCommit = { key: null, at: 0 }
      set({
        projectId: id,
        project: null,
        status: 'loading',
        error: null,
        geometry: null,
        geometrySig: null,
        geometryLoading: false,
        geometryError: null,
        past: [],
        future: [],
        rev: 0,
        savedRev: 0,
        saveState: 'saved',
        saveError: null,
        selection: emptySelection,
        busy: null,
      })
      try {
        const project = await projectsApi.get(id)
        if (token !== openToken) return
        const top = project.layers[project.layers.length - 1]
        set({
          project,
          status: 'ready',
          lastSavedAt: Date.parse(project.updatedAt) || Date.now(),
          selection: top ? { layerIds: [top.id], primary: top.id, elementIds: [] } : emptySelection,
          editSeq: get().editSeq + 1,
        })
        void get().refreshGeometry(true)
      } catch (e) {
        if (token !== openToken) return
        set({ status: 'error', error: errorMessage(e) })
      }
    },

    close: () => {
      const s = get()
      // Unsaved edits must survive leaving the editor even while an older PUT is still in flight
      // (flushSave would wait for it and then find no project any more).
      const pending = s.project && s.rev !== s.savedRev ? s.project : null
      window.clearTimeout(saveTimer)
      window.clearTimeout(retryTimer)
      openToken++
      geoToken++
      lastCommit = { key: null, at: 0 }
      set({
        projectId: null,
        project: null,
        status: 'idle',
        geometry: null,
        geometrySig: null,
        geometryLoading: false,
        past: [],
        future: [],
        selection: emptySelection,
        busy: null,
      })
      if (pending) void saveDetached(pending)
    },

    commit: (fn, opts = {}) => {
      const s = get()
      if (!s.project) return
      // A server-side structural op is replacing the project; edits made now would be lost.
      if (s.busy && opts.history !== false) return
      const next = fn(s.project)
      if (next === s.project) return
      const { coalesce, history = true, render = true } = opts
      const now = performance.now()
      const merge = !!coalesce && lastCommit.key === coalesce && (gestureActive || now - lastCommit.at < COALESCE_MS)
      lastCommit = { key: coalesce ?? null, at: now }
      set({
        project: next,
        past: history && !merge ? [...s.past, s.project].slice(-HISTORY_LIMIT) : s.past,
        future: history ? [] : s.future,
        rev: s.rev + 1,
        saveState: s.saveState === 'saving' ? 'saving' : 'dirty',
        editSeq: render ? s.editSeq + 1 : s.editSeq,
        selection: pruneSelection(s.selection, next),
      })
      scheduleSave()
    },

    undo: () => {
      const s = get()
      if (!s.project || !s.past.length || s.busy) return
      const prev = s.past[s.past.length - 1]
      const restored: Project = { ...prev, appearance: s.project.appearance }
      lastCommit = { key: null, at: 0 }
      set({
        project: restored,
        past: s.past.slice(0, -1),
        future: [s.project, ...s.future].slice(0, HISTORY_LIMIT),
        rev: s.rev + 1,
        saveState: 'dirty',
        editSeq: s.editSeq + 1,
        selection: pruneSelection(s.selection, restored),
      })
      scheduleSave(200)
    },

    redo: () => {
      const s = get()
      if (!s.project || !s.future.length || s.busy) return
      const [nextP, ...rest] = s.future
      const restored: Project = { ...nextP, appearance: s.project.appearance }
      lastCommit = { key: null, at: 0 }
      set({
        project: restored,
        past: [...s.past, s.project].slice(-HISTORY_LIMIT),
        future: rest,
        rev: s.rev + 1,
        saveState: 'dirty',
        editSeq: s.editSeq + 1,
        selection: pruneSelection(s.selection, restored),
      })
      scheduleSave(200)
    },

    flushSave: async () => {
      window.clearTimeout(saveTimer)
      window.clearTimeout(retryTimer)
      // Serialize: wait for an in-flight PUT, then save again if there are newer edits.
      for (let guard = 0; guard < 8; guard++) {
        if (inflight) {
          await inflight
          continue
        }
        const s = get()
        if (!s.project) return true
        if (s.rev === s.savedRev) {
          if (s.saveState !== 'saved') set({ saveState: 'saved' })
          return true
        }
        const rev = s.rev
        const snapshot = s.project
        set({ saveState: 'saving' })
        let failed = false
        inflight = (async () => {
          try {
            await projectsApi.save(snapshot)
            if (get().project?.id !== snapshot.id) return
            const cur = get()
            set({
              savedRev: Math.max(cur.savedRev, rev),
              saveState: cur.rev === rev ? 'saved' : 'dirty',
              saveError: null,
              lastSavedAt: Date.now(),
            })
          } catch (e) {
            failed = true
            if (get().project?.id !== snapshot.id) return
            const msg = errorMessage(e)
            set({ saveState: 'error', saveError: msg })
            toast.error('Autosave failed', { id: 'autosave-error', description: `${msg} — retrying…` })
            window.clearTimeout(retryTimer)
            retryTimer = window.setTimeout(() => void get().flushSave(), 4000)
          } finally {
            inflight = null
          }
        })()
        await inflight
        if (failed) return false
        if (get().rev === get().savedRev) {
          // Saved: if the structure changed since the geometry we hold, rebuild it.
          const p = get().project
          if (p && structuralSig(p) !== get().geometrySig && !get().geometryLoading) void get().refreshGeometry()
          return true
        }
      }
      return get().rev === get().savedRev
    },

    refreshGeometry: async (force) => {
      const p0 = get().project
      if (!p0) return
      const token = ++geoToken
      set({ geometryLoading: true, geometryError: null })
      if (get().rev !== get().savedRev) {
        const ok = await get().flushSave()
        if (!ok || token !== geoToken) {
          if (token === geoToken) set({ geometryLoading: false })
          return
        }
      }
      const p = get().project
      if (!p) return
      const sig = structuralSig(p)
      if (!force && sig === get().geometrySig && get().geometry) {
        set({ geometryLoading: false })
        return
      }
      try {
        const g = await projectsApi.geometry(p.id)
        if (token !== geoToken || get().project?.id !== p.id) return
        set({ geometry: g, geometrySig: sig, geometryLoading: false })
        // The structure may have changed again while we were fetching.
        const cur = get().project
        if (cur && structuralSig(cur) !== sig && get().rev === get().savedRev) void get().refreshGeometry()
      } catch (e) {
        if (token !== geoToken) return
        set({ geometryLoading: false, geometryError: errorMessage(e) })
      }
    },

    // ---------------------------------------------------------------------------------- selection
    selectLayer: (id, mode = 'replace') => {
      const { project, selection } = get()
      if (!project) return
      if (id == null) {
        set({ selection: emptySelection })
        return
      }
      if (mode === 'toggle') {
        const has = selection.layerIds.includes(id)
        const layerIds = has ? selection.layerIds.filter((x) => x !== id) : [...selection.layerIds, id]
        set({ selection: { layerIds, primary: has ? (layerIds[layerIds.length - 1] ?? null) : id, elementIds: [] } })
        return
      }
      if (mode === 'range' && selection.primary) {
        const order = project.layers.map((l) => l.id)
        const a = order.indexOf(selection.primary)
        const b = order.indexOf(id)
        if (a >= 0 && b >= 0) {
          const [lo, hi] = a < b ? [a, b] : [b, a]
          set({ selection: { layerIds: order.slice(lo, hi + 1), primary: selection.primary, elementIds: [] } })
          return
        }
      }
      set({ selection: { layerIds: [id], primary: id, elementIds: [] } })
    },

    selectLayers: (ids) => set({ selection: { layerIds: ids, primary: ids[ids.length - 1] ?? null, elementIds: [] } }),

    selectElement: (id, mode = 'replace') => {
      const { project, selection } = get()
      if (!project) return
      const layer = project.layers.find((l) => l.elementIds.includes(id))
      if (!layer) return
      let elementIds: string[]
      if (mode === 'toggle') {
        elementIds = selection.elementIds.includes(id)
          ? selection.elementIds.filter((x) => x !== id)
          : [...selection.elementIds, id]
      } else if (mode === 'range' && selection.elementIds.length) {
        const order = project.layers.flatMap((l) => [...l.elementIds].reverse())
        const a = order.indexOf(selection.elementIds[selection.elementIds.length - 1])
        const b = order.indexOf(id)
        const [lo, hi] = a < b ? [a, b] : [b, a]
        elementIds = a >= 0 && b >= 0 ? order.slice(lo, hi + 1) : [id]
      } else {
        elementIds = [id]
      }
      set({ selection: { layerIds: [layer.id], primary: layer.id, elementIds } })
    },

    clearSelection: () => set({ selection: emptySelection }),

    // ---------------------------------------------------------------------------------- simple edits
    setAppearance: (a) => {
      if (get().project?.appearance === a) return
      get().commit((p) => ({ ...p, appearance: a }), { history: false, render: false })
    },

    rename: (name) => {
      const n = name.trim()
      if (!n || n === get().project?.name) return
      get().commit((p) => ({ ...p, name: n }), { render: false })
    },

    reorder: (fromIndex, toIndex) => get().commit((p) => reorderLayers(p, fromIndex, toIndex)),

    deleteLayers: (ids) => {
      const s = get()
      const target = (ids ?? s.selection.layerIds).filter((id) => !s.project?.layers.find((l) => l.id === id)?.locked)
      if (!s.project || !target.length) return
      if (target.length >= s.project.layers.length) {
        toast.warning('Keep at least one layer', { description: 'Use re-split to rebuild the layer stack instead.' })
        return
      }
      get().commit((p) => removeLayers(p, target))
      toast.info(target.length === 1 ? 'Layer deleted' : `${target.length} layers deleted`, {
        action: { label: 'Undo', onClick: () => get().undo() },
      })
    },

    nudge: (dx, dy) => {
      const s = get()
      if (!s.project || !s.selection.layerIds.length) return
      const ids = s.selection.layerIds.filter((id) => !s.project!.layers.find((l) => l.id === id)?.locked)
      get().commit(
        (p) =>
          updateLayers(p, ids, (l) => ({
            ...l,
            transform: {
              ...l.transform,
              x: Math.round((l.transform.x + dx) * 10000) / 10000,
              y: Math.round((l.transform.y + dy) * 10000) / 10000,
            },
          })),
        { coalesce: 'nudge' },
      )
    },

    // ---------------------------------------------------------------------------------- structural
    resplit: (strategy) =>
      structural(`Re-split (${strategy})`, (id) => projectsApi.split(id, strategy), (p) => {
        const top = p.layers[p.layers.length - 1]
        return { selection: top ? { layerIds: [top.id], primary: top.id, elementIds: [] } : emptySelection }
      }),

    mergeLayers: async (ids) => {
      const s = get()
      const target = ids ?? s.selection.layerIds
      if (!s.project || target.length < 2) {
        toast.info('Select two or more layers to merge', { description: 'Ctrl/Shift-click layers in the Layers panel.' })
        return
      }
      const order = s.project.layers.map((l) => l.id)
      const sorted = [...target].sort((a, b) => order.indexOf(a) - order.indexOf(b))
      const prevIds = new Set(order)
      await structural('Merge layers', (id) => projectsApi.mergeLayers(id, sorted), (p) => {
        const created = p.layers.find((l) => !prevIds.has(l.id)) ?? p.layers.find((l) => l.id === sorted[0])
        return { selection: created ? { layerIds: [created.id], primary: created.id, elementIds: [] } : emptySelection }
      })
    },

    splitLayer: async (layerId, mode) => {
      const prevIds = new Set(get().project?.layers.map((l) => l.id) ?? [])
      await structural(mode === 'islands' ? 'Split into islands' : 'Split into elements', (id) => projectsApi.splitLayer(id, layerId, mode), (p) => {
        const created = p.layers.filter((l) => !prevIds.has(l.id)).map((l) => l.id)
        return created.length ? { selection: { layerIds: created, primary: created[created.length - 1], elementIds: [] } } : {}
      })
    },

    moveElements: async (elementIds, toLayerId) => {
      if (!elementIds.length) return
      const prevIds = new Set(get().project?.layers.map((l) => l.id) ?? [])
      await structural(toLayerId ? 'Move elements' : 'New layer from elements', (id) => projectsApi.moveElements(id, elementIds, toLayerId), (p) => {
        const target = toLayerId ?? p.layers.find((l) => !prevIds.has(l.id))?.id
        return target ? { selection: { layerIds: [target], primary: target, elementIds } } : {}
      })
    },
  }
})

// Flush pending edits when the tab is closing. keepalive requests survive the unload but browsers cap their
// body at 64 KB — projects with many elements are bigger, so those take a normal save (it completes while
// the "leave page?" prompt is shown).
const KEEPALIVE_MAX = 60_000
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', (e) => {
    const s = useEditor.getState()
    if (!s.project || s.rev === s.savedRev) return
    const size = JSON.stringify(s.project).length
    if (size < KEEPALIVE_MAX && !inflight) {
      const { id } = s.project
      const rev = s.rev
      void projectsApi
        .save(s.project, { keepalive: true })
        .then(() => {
          // Still here (the user cancelled leaving): reflect the save.
          const cur = useEditor.getState()
          if (cur.project?.id === id && cur.savedRev < rev)
            useEditor.setState({ savedRev: rev, saveState: cur.rev === rev ? 'saved' : 'dirty', lastSavedAt: Date.now() })
        })
        .catch(() => undefined)
    } else {
      void s.flushSave()
    }
    e.preventDefault()
  })
}

// ------------------------------------------------------------------------------------------ selectors
export const selectPrimaryLayer = (s: EditorState) =>
  s.project && s.selection.primary ? (s.project.layers.find((l) => l.id === s.selection.primary) ?? null) : null
