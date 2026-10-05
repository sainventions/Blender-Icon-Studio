// UI preferences (persisted to localStorage): panel sizes, stage mode, viewport toggles, inspector tab…
import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'
import { safeStorage } from '../lib/hooks'
import type { StageView } from '../features/editor/stage/view'

export type StageMode = 'viewport' | 'render' | 'compare' | 'matrix'
export type InspectorTab = 'layer' | 'document' | 'render'
export type ScopeSection = 'color' | 'material' | 'plate'
export type Scope = 'all' | 'appearance'

export const PANEL_LIMITS = { left: [200, 420], right: [272, 460] } as const
export const PANEL_DEFAULTS = { left: 256, right: 316 } as const

interface UiState {
  leftWidth: number
  rightWidth: number
  leftCollapsed: boolean
  rightCollapsed: boolean
  stageMode: StageMode
  view3d: 'front' | 'orbit'
  /** Transient camera.iso while the View control animates (Front / Iso buttons, I key); null = the project's value.
   *  The final value is committed to project.camera.iso once, at the end. Not persisted. */
  isoAnim: number | null
  showGrid: boolean
  /** CAD-style stage view (zoom at the cursor / pan; features/editor/stage/view.ts) shared by Live, Render and
   *  Compare. A VIEW transform only, never the project's render framing (camera.zoom). null = Fit. Not persisted. */
  stageView: StageView | null
  /** Live / Blender divider of the Compare view, as a fraction of the stage width. */
  compareSplit: number
  inspectorTab: InspectorTab
  scopes: Record<ScopeSection, Scope>
  collapsedSections: Record<string, boolean>
  renditionsOpen: boolean
  matrixQuality: 'draft' | 'preview'
  sampleQuery: string
  dialog: null | 'export' | 'animate' | 'import' | 'shortcuts'
  importFile: File | null

  set: (patch: Partial<UiState>) => void
  setScope: (section: ScopeSection, scope: Scope) => void
  toggleSection: (id: string) => void
  openDialog: (d: UiState['dialog'], file?: File | null) => void
}

const storage: StateStorage = {
  getItem: (k) => safeStorage.get(k),
  setItem: (k, v) => safeStorage.set(k, v),
  removeItem: (k) => safeStorage.remove(k),
}

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      leftWidth: PANEL_DEFAULTS.left,
      rightWidth: PANEL_DEFAULTS.right,
      leftCollapsed: false,
      rightCollapsed: false,
      stageMode: 'viewport',
      view3d: 'front',
      isoAnim: null,
      showGrid: false,
      stageView: null,
      compareSplit: 0.5,
      inspectorTab: 'layer',
      scopes: { color: 'appearance', material: 'all', plate: 'appearance' },
      collapsedSections: {},
      renditionsOpen: true,
      matrixQuality: 'draft',
      sampleQuery: '',
      dialog: null,
      importFile: null,

      set: (patch) => set(patch),
      setScope: (section, scope) => set((s) => ({ scopes: { ...s.scopes, [section]: scope } })),
      toggleSection: (id) =>
        set((s) => ({ collapsedSections: { ...s.collapsedSections, [id]: !s.collapsedSections[id] } })),
      openDialog: (dialog, file = null) => set({ dialog, importFile: file }),
    }),
    {
      name: 'bis.ui.v1',
      storage: createJSONStorage(() => storage),
      partialize: (s) => ({
        leftWidth: s.leftWidth,
        rightWidth: s.rightWidth,
        leftCollapsed: s.leftCollapsed,
        rightCollapsed: s.rightCollapsed,
        stageMode: s.stageMode,
        view3d: s.view3d,
        showGrid: s.showGrid,
        compareSplit: s.compareSplit,
        inspectorTab: s.inspectorTab,
        scopes: s.scopes,
        collapsedSections: s.collapsedSections,
        renditionsOpen: s.renditionsOpen,
        matrixQuality: s.matrixQuality,
      }),
    },
  ),
)
