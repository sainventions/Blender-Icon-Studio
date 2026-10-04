import { useEffect, useRef } from 'react'
import { ArrowLeft, FileUp, FileWarning, RotateCw } from 'lucide-react'
import { goHome } from '../../lib/route'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { startRenderAutomation, useRender } from '../../store/render'
import { PANEL_DEFAULTS, PANEL_LIMITS, useUi } from '../../store/ui'
import { clamp, cn } from '../../lib/format'
import { Button, EmptyState, ResizeHandle } from '../../components/ui'
import { EditorSkeleton } from './EditorSkeleton'
import { TopBar } from './TopBar'
import { StatusBar } from './StatusBar'
import { LayersPanel } from './layers/LayersPanel'
import { Stage } from './stage/Stage'
import { Inspector } from './inspector/Inspector'
import { useEditorShortcuts } from './shortcuts'
import { ExportDialog } from '../dialogs/ExportDialog'
import { AnimateDialog } from '../dialogs/AnimateDialog'
import { useFileDrop } from '../home/useFileDrop'
import { installStackKeeper } from './stacking'

// a real-height layer stack stays one across client-side edits (PLAN §11 round 7; features/editor/stacking)
installStackKeeper()

export default function EditorPage({ projectId }: { projectId: string }) {
  const status = useEditor((s) => s.status)
  const error = useEditor((s) => s.error)
  const presets = useAppStore((s) => s.presets.data)
  const presetsError = useAppStore((s) => s.presets.error)
  const loadPresets = useAppStore((s) => s.loadPresets)
  const name = useEditor((s) => s.project?.name)

  useEffect(() => {
    useRender.getState().reset(projectId)
    void useEditor.getState().open(projectId)
    const stop = startRenderAutomation()
    return () => {
      stop()
      useEditor.getState().close()
      useRender.getState().reset(null)
    }
  }, [projectId])

  useEffect(() => {
    if (presets) return
    void loadPresets()
    // Keep retrying while the server is unreachable (the editor cannot render without presets.json).
    const t = window.setInterval(() => void loadPresets(), 3000)
    return () => window.clearInterval(t)
  }, [presets, loadPresets])

  useEffect(() => {
    if (name) document.title = `${name} · Blender Icon Studio`
  }, [name])

  useEditorShortcuts()

  if (status === 'error') {
    return (
      <div className="flex h-full items-center justify-center bg-app">
        <EmptyState
          icon={<FileWarning />}
          title="This project could not be opened"
          action={
            <div className="flex gap-2">
              <Button variant="secondary" icon={<ArrowLeft />} onClick={goHome}>
                Back to projects
              </Button>
              <Button variant="primary" icon={<RotateCw />} onClick={() => void useEditor.getState().open(projectId)}>
                Retry
              </Button>
            </div>
          }
        >
          {error}
        </EmptyState>
      </div>
    )
  }
  if (status !== 'ready' || !presets) {
    return <EditorSkeleton label={presetsError && !presets ? `Waiting for the server… (${presetsError})` : 'Opening project…'} />
  }
  return <EditorLayout />
}

function EditorLayout() {
  const leftWidth = useUi((s) => s.leftWidth)
  const rightWidth = useUi((s) => s.rightWidth)
  const leftCollapsed = useUi((s) => s.leftCollapsed)
  const rightCollapsed = useUi((s) => s.rightCollapsed)
  const setUi = useUi((s) => s.set)
  const startW = useRef(0)
  // Dropping another SVG onto the editor starts a new import (never let the browser navigate to the file).
  const dragging = useFileDrop((file) => useUi.getState().openDialog('import', file))

  return (
    <div className="flex h-full flex-col bg-app">
      <TopBar />
      <div className="relative flex min-h-0 flex-1">
        <aside
          className={cn(
            'relative z-10 flex shrink-0 flex-col border-r border-line panel transition-[width,opacity] duration-200',
            leftCollapsed && 'pointer-events-none w-0 overflow-hidden border-r-0 opacity-0',
          )}
          style={leftCollapsed ? undefined : { width: leftWidth }}
        >
          <LayersPanel />
          {!leftCollapsed && (
            <ResizeHandle
              side="left"
              onResize={(d, phase) => {
                // 'end' carries no delta — applying it would snap the panel back to its start width.
                if (phase === 'start') startW.current = useUi.getState().leftWidth
                else if (phase === 'move') setUi({ leftWidth: clamp(startW.current + d, PANEL_LIMITS.left[0], PANEL_LIMITS.left[1]) })
              }}
              onReset={() => setUi({ leftWidth: PANEL_DEFAULTS.left })}
            />
          )}
        </aside>

        <Stage />

        <aside
          className={cn(
            'relative z-10 flex shrink-0 flex-col border-l border-line panel transition-[width,opacity] duration-200',
            rightCollapsed && 'pointer-events-none w-0 overflow-hidden border-l-0 opacity-0',
          )}
          style={rightCollapsed ? undefined : { width: rightWidth }}
        >
          {!rightCollapsed && (
            <ResizeHandle
              side="right"
              onResize={(d, phase) => {
                if (phase === 'start') startW.current = useUi.getState().rightWidth
                else if (phase === 'move') setUi({ rightWidth: clamp(startW.current + d, PANEL_LIMITS.right[0], PANEL_LIMITS.right[1]) })
              }}
              onReset={() => setUi({ rightWidth: PANEL_DEFAULTS.right })}
            />
          )}
          <Inspector />
        </aside>
      </div>
      <StatusBar />
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-[700] flex items-center justify-center bg-app/60 backdrop-blur-sm animate-fade-in">
          <div className="flex items-center gap-3 rounded-2xl border border-accent/50 bg-surface-1/90 px-6 py-4 text-sm font-semibold text-fg shadow-pop">
            <FileUp className="h-5 w-5 text-accent" /> Drop to import as a new project
          </div>
        </div>
      )}
      <ExportDialog />
      <AnimateDialog />
    </div>
  )
}
