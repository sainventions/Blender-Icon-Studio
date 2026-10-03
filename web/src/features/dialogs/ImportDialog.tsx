// Import: pick/drop an SVG, choose a split strategy, preview the auto-split as an exploded stack, then open.
import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, FileUp, Layers, LoaderCircle, Trash2, TriangleAlert, Upload, WandSparkles } from 'lucide-react'
import type { Project, SplitStrategy } from '../../types'
import { errorMessage, projectsApi } from '../../api'
import { cn, formatBytes, hashString } from '../../lib/format'
import { fillToCss } from '../../lib/color'
import { STRATEGIES } from '../../lib/meta'
import { openProjectRoute } from '../../lib/route'
import { useAppStore } from '../../store/app'
import { toast } from '../../store/toasts'
import { useUi } from '../../store/ui'
import { Badge, Button, Dialog } from '../../components/ui'
import { SourcePlateBadge } from '../../components/SourcePlateBadge'
import { isSvgFile } from '../home/useFileDrop'

export function ImportDialog() {
  const open = useUi((s) => s.dialog === 'import')
  const initialFile = useUi((s) => s.importFile)
  const [file, setFile] = useState<File | null>(null)
  const [strategy, setStrategy] = useState<SplitStrategy>('smart')
  const [project, setProject] = useState<Project | null>(null)
  const [busy, setBusy] = useState<null | 'import' | 'split' | 'discard'>(null)
  const [hover, setHover] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [preview, setPreview] = useState<string | null>(null)

  // Object URL owned by the effect (created and revoked in the same place — safe under StrictMode re-runs).
  useEffect(() => {
    if (!file) {
      setPreview(null)
      return
    }
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  useEffect(() => {
    if (open) {
      setFile(initialFile)
      setProject(null)
      setBusy(null)
    }
  }, [open, initialFile])

  const close = () => {
    useUi.getState().openDialog(null)
    if (project) void useAppStore.getState().loadProjects()
  }

  const doImport = async () => {
    if (!file) return
    setBusy('import')
    try {
      const p = await projectsApi.upload(file, file.name, strategy)
      setProject(p)
    } catch (e) {
      toast.error('Import failed', { description: errorMessage(e) })
    } finally {
      setBusy(null)
    }
  }

  const resplit = async (s: SplitStrategy) => {
    setStrategy(s)
    if (!project) return
    setBusy('split')
    try {
      setProject(await projectsApi.split(project.id, s))
    } catch (e) {
      toast.error('Re-split failed', { description: errorMessage(e) })
    } finally {
      setBusy(null)
    }
  }

  const discard = async () => {
    if (!project) return
    setBusy('discard')
    try {
      await projectsApi.remove(project.id)
    } catch {
      /* already gone */
    }
    setProject(null)
    setBusy(null)
  }

  const openEditor = () => {
    if (!project) return
    useUi.getState().openDialog(null)
    openProjectRoute(project.id)
  }

  const pick = (f: File | undefined | null) => {
    if (!f) return
    if (!isSvgFile(f)) {
      toast.warning('Please choose an .svg file', { description: f.name })
      return
    }
    setFile(f)
    setProject(null)
  }

  return (
    <Dialog
      open={open}
      onClose={close}
      width={860}
      icon={<FileUp />}
      title={project ? 'Review the layer split' : 'Import SVG'}
      description={project ? `${project.layers.length} layers from ${project.elements.length} elements — change the strategy until the stack looks right.` : 'Each layer becomes an extruded depth plane with its own material.'}
      footer={
        project ? (
          <>
            <Button variant="ghost" icon={<Trash2 />} loading={busy === 'discard'} onClick={() => void discard()}>
              Discard
            </Button>
            <div className="flex-1" />
            <Button variant="secondary" onClick={close}>
              Keep for later
            </Button>
            <Button variant="primary" iconRight={<ArrowRight />} onClick={openEditor} disabled={!!busy} data-autofocus>
              Open in editor
            </Button>
          </>
        ) : (
          <>
            <div className="flex-1" />
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" icon={<WandSparkles />} disabled={!file} loading={busy === 'import'} onClick={() => void doImport()} data-autofocus>
              Import & split
            </Button>
          </>
        )
      }
    >
      <div className="grid grid-cols-[300px_1fr] gap-5">
        {/* left: source / stack preview */}
        <div className="space-y-3">
          {project ? (
            <StackPreview project={project} hover={hover} busy={busy === 'split'} />
          ) : (
            <div
              onDragOver={(e) => {
                e.preventDefault()
                e.stopPropagation()
              }}
              onDrop={(e) => {
                e.preventDefault()
                e.stopPropagation()
                pick(e.dataTransfer.files?.[0])
              }}
              onClick={() => inputRef.current?.click()}
              className={cn(
                'relative flex aspect-square cursor-pointer flex-col items-center justify-center gap-3 overflow-hidden rounded-2xl border border-dashed transition-colors',
                file ? 'border-line-2 checkerboard' : 'border-line-3 bg-white/[0.02] hover:border-accent/60 hover:bg-accent/[0.04]',
              )}
            >
              {preview ? (
                <img src={preview} alt="" className="absolute inset-[10%] h-[80%] w-[80%] object-contain drop-shadow-[0_12px_24px_rgb(0_0_0/0.5)]" />
              ) : (
                <>
                  <div className="flex h-12 w-12 items-center justify-center rounded-2xl accent-gradient-soft ring-1 ring-inset ring-white/10">
                    <Upload className="h-5 w-5 text-fg" />
                  </div>
                  <div className="text-center">
                    <div className="text-xs font-semibold text-fg-2">Drop an SVG here</div>
                    <div className="text-2xs text-fg-4">or click to browse</div>
                  </div>
                </>
              )}
            </div>
          )}
          {file && !project && (
            <div className="flex items-center gap-2 rounded-lg border border-line bg-surface-0/50 px-2.5 py-2">
              <FileUp className="h-3.5 w-3.5 text-fg-3" />
              <span className="min-w-0 flex-1 truncate text-2xs font-medium text-fg-2">{file.name}</span>
              <span className="text-3xs text-fg-4">{formatBytes(file.size)}</span>
              <button type="button" onClick={() => inputRef.current?.click()} className="text-3xs font-semibold text-accent hover:underline">
                Change
              </button>
            </div>
          )}
          <input
            ref={inputRef}
            type="file"
            accept=".svg,image/svg+xml"
            className="hidden"
            onChange={(e) => {
              pick(e.target.files?.[0])
              e.target.value = ''
            }}
          />
        </div>

        {/* right: strategy + layers */}
        <div className="min-w-0 space-y-4">
          <div>
            <div className="mb-2 text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">Split strategy</div>
            <div className="grid grid-cols-1 gap-1.5">
              {STRATEGIES.map((s) => {
                const on = (project?.strategy ?? strategy) === s.id
                return (
                  <button
                    key={s.id}
                    type="button"
                    disabled={!!busy}
                    onClick={() => void resplit(s.id)}
                    className={cn(
                      'flex items-center gap-3 rounded-lg border px-3 py-2 text-left transition-[border-color,background-color]',
                      on ? 'border-accent/55 bg-accent/[0.09]' : 'border-line bg-surface-0/40 hover:border-line-2',
                    )}
                  >
                    <span className={cn('flex h-4 w-4 shrink-0 items-center justify-center rounded-full border', on ? 'border-transparent accent-gradient' : 'border-line-3')}>
                      {on && <Check className="h-2.5 w-2.5 text-white" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-xs font-semibold text-fg">
                        {s.label}
                        {s.id === 'smart' && <Badge tone="accent" className="ml-1.5">Recommended</Badge>}
                      </span>
                      <span className="block text-3xs text-fg-4">{s.description}</span>
                    </span>
                    {busy === 'split' && on && <LoaderCircle className="h-3.5 w-3.5 animate-spin text-fg-3" />}
                  </button>
                )
              })}
            </div>
          </div>

          {project && (
            <div className="animate-fade-in">
              <div className="mb-2 flex items-center gap-2">
                <span className="text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">Layers (top → bottom)</span>
                {(project.source.plateDetected || project.source.fullBleed) && <SourcePlateBadge source={project.source} />}
                {project.layers.length > 4 && <Badge tone="warn" tip="Apple Icon Composer allows at most 4 groups">{project.layers.length} &gt; 4 groups</Badge>}
              </div>
              <div className="max-h-[220px] space-y-1 overflow-y-auto pr-1">
                {[...project.layers].reverse().map((l) => (
                  <div
                    key={l.id}
                    onPointerEnter={() => setHover(l.id)}
                    onPointerLeave={() => setHover(null)}
                    className={cn('flex items-center gap-2.5 rounded-lg border px-2 py-1.5 transition-colors', hover === l.id ? 'border-accent/50 bg-accent/[0.06]' : 'border-line')}
                  >
                    <div className="relative h-8 w-8 shrink-0 overflow-hidden rounded-md border border-line-2 checkerboard-sm">
                      <img src={projectsApi.layerThumbnailUrl(project.id, l.id, hashString(l.elementIds.join(',')))} alt="" className="absolute inset-0 h-full w-full object-contain" />
                    </div>
                    <span className="min-w-0 flex-1 truncate text-xs font-medium text-fg-2">{l.name}</span>
                    <span className="text-3xs text-fg-4">{l.elementIds.length} el</span>
                  </div>
                ))}
              </div>
              {(project.source.warnings ?? []).length > 0 && (
                <div className="mt-3 space-y-1 rounded-lg border border-warn/20 bg-warn/[0.06] p-2">
                  {project.source.warnings.slice(0, 4).map((w, i) => (
                    <div key={i} className="flex items-start gap-1.5 text-3xs leading-snug text-warn/90">
                      <TriangleAlert className="mt-px h-3 w-3 shrink-0" /> {w}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </Dialog>
  )
}

/** Isometric exploded stack of the layer thumbnails above the plate. */
function StackPreview({ project, hover, busy }: { project: Project; hover: string | null; busy: boolean }) {
  const n = project.layers.length
  const gap = Math.max(18, Math.min(40, 150 / Math.max(1, n)))
  return (
    <div className="relative flex aspect-square items-center justify-center overflow-hidden rounded-2xl border border-line bg-[radial-gradient(ellipse_at_50%_40%,rgb(143_125_255/0.14),transparent_70%)]">
      <div style={{ perspective: 900 }} className="relative h-[62%] w-[62%]">
        <div className="absolute inset-0" style={{ transformStyle: 'preserve-3d', transform: `rotateX(58deg) rotateZ(-38deg) translateZ(${-gap * n * 0.35}px)` }}>
          {project.canvas.plate.visible && project.canvas.shape !== 'none' && (
            <div
              className="absolute inset-0 rounded-[22%] border border-white/20 shadow-[0_30px_60px_-20px_rgb(0_0_0/0.8)]"
              style={{ background: fillToCss(project.canvas.plate.fill, '#ddd'), transform: 'translateZ(0px)' }}
            />
          )}
          {project.layers.map((l, i) => (
            <div
              key={l.id}
              className={cn('absolute inset-0 rounded-[22%] border transition-[border-color,box-shadow,opacity] duration-200', hover === l.id ? 'border-accent shadow-[0_0_24px_rgb(143_125_255/0.6)]' : 'border-white/10', hover && hover !== l.id && 'opacity-40')}
              style={{ transform: `translateZ(${(i + 1) * gap}px)`, background: 'rgb(255 255 255 / 0.03)' }}
            >
              <img
                src={projectsApi.layerThumbnailUrl(project.id, l.id, hashString(l.elementIds.join(',')))}
                alt=""
                className="h-full w-full object-contain"
                draggable={false}
              />
            </div>
          ))}
        </div>
      </div>
      <div className="absolute bottom-2 left-2 flex items-center gap-1.5 rounded-full bg-black/40 px-2 py-0.5 text-3xs text-fg-2 backdrop-blur">
        <Layers className="h-3 w-3" /> {n} layer{n === 1 ? '' : 's'}
      </div>
      {busy && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <LoaderCircle className="h-6 w-6 animate-spin text-white" />
        </div>
      )}
    </div>
  )
}
