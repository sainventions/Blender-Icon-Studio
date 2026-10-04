import { useEffect, useMemo, useState } from 'react'
import { Check, CircleAlert, Download, FileArchive, FileImage, LoaderCircle, PackageCheck, X } from 'lucide-react'
import type { AppearanceId, ExportTarget, Quality } from '../../types'
import { errorMessage, jobsApi, projectsApi } from '../../api'
import { cn, formatSeconds } from '../../lib/format'
import { APPEARANCE_IDS } from '../../lib/projectOps'
import { APPEARANCE_VISUAL, EXPORT_TARGETS } from '../../lib/meta'
import { appearanceLabel } from '../../lib/labels'
import { safeStorage } from '../../lib/hooks'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { toast } from '../../store/toasts'
import { useUi } from '../../store/ui'
import { AppearanceIcon, PlatformIcon } from '../../components/icons'
import { Badge, Button, Dialog, ProgressBar, Segmented } from '../../components/ui'
import { continueInToast, isActive, useJob } from './useJob'

const PREFS_KEY = 'bis.export.v1'
export interface ExportPrefs {
  targets: ExportTarget[]
  appearances: AppearanceId[]
  quality: Quality
}
type Prefs = ExportPrefs
const DEFAULT_PREFS: Prefs = { targets: ['ios', 'macos', 'web'], appearances: ['light', 'dark', 'tinted-dark'], quality: 'final' }

/** The Export dialog's remembered choices (the Icon Pack page starts from them too). */
export function loadExportPrefs(): ExportPrefs {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(safeStorage.get(PREFS_KEY) ?? '{}') }
  } catch {
    return DEFAULT_PREFS
  }
}

const EXPORT_TOAST: Parameters<typeof continueInToast>[1] = {
  title: 'Exporting icons',
  done: 'Export ready',
  doneDescription: (j) => (Array.isArray(j.result?.files) ? `${j.result.files.length} files packaged` : undefined),
  links: (j) => (typeof j.result?.zip === 'string' ? [{ label: 'Download .zip', href: j.result.zip, download: true }] : undefined),
}

export function ExportDialog() {
  const open = useUi((s) => s.dialog === 'export')
  const project = useEditor((s) => s.project)
  const presets = useAppStore((s) => s.presets.data)
  const [prefs, setPrefs] = useState<Prefs>(loadExportPrefs)
  const [jobId, setJobId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const job = useJob(jobId)
  const close = () => {
    continueInToast(job, EXPORT_TOAST)
    useUi.getState().openDialog(null)
  }

  useEffect(() => safeStorage.set(PREFS_KEY, JSON.stringify(prefs)), [prefs])
  useEffect(() => {
    if (open && job && !isActive(job) && job.state !== 'done') setJobId(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const est = useMemo(() => {
    const per = prefs.quality === 'ultra' ? 40 : prefs.quality === 'final' ? 8 : prefs.quality === 'preview' ? 1.5 : 0.4
    // marketing (server export.plan_export): hero (3/4 CAD view) + isometric hero of the primary appearance, plus a
    // dark hero when Dark is exported alongside another primary
    const apps = prefs.appearances
    const primary = apps.includes('light') ? 'light' : apps[0]
    const marketing = prefs.targets.includes('marketing') ? 2 + (apps.includes('dark') && primary !== 'dark' ? 1 : 0) : 0
    const renders = Math.max(1, prefs.appearances.length) + (prefs.targets.includes('android') ? 2 : 0) + (prefs.targets.includes('watchos') ? 1 : 0) + marketing
    return per * renders
  }, [prefs])

  if (!project) return null
  const running = isActive(job)
  const done = job?.state === 'done'
  const files = (job?.result?.files as { name: string; url: string }[] | undefined) ?? []
  const zip = typeof job?.result?.zip === 'string' ? job.result.zip : typeof job?.result?.url === 'string' ? job.result.url : null

  const start = async () => {
    setSubmitting(true)
    try {
      await useEditor.getState().flushSave()
      const j = await projectsApi.export(project.id, { targets: prefs.targets, appearances: prefs.appearances, quality: prefs.quality })
      useAppStore.getState().upsertJob(j)
      setJobId(j.id)
      // Closed while the request was in flight: report the job in a toast instead.
      if (useUi.getState().dialog !== 'export') continueInToast(useAppStore.getState().jobs[j.id] ?? j, EXPORT_TOAST, { finished: true })
    } catch (e) {
      toast.error('Export failed to start', { description: errorMessage(e) })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={close}
      width={720}
      icon={<PackageCheck />}
      title="Export icons"
      description={`${project.name} — rendered with Blender, resized and packaged for every platform you pick.`}
      footer={
        job ? (
          <>
            {running && (
              <Button variant="ghost" icon={<X />} onClick={() => void jobsApi.cancel(job.id)}>
                Cancel export
              </Button>
            )}
            <div className="flex-1" />
            {!running && (
              <Button variant="secondary" onClick={() => setJobId(null)}>
                {done ? 'Export again' : 'Back'}
              </Button>
            )}
            {done && zip && (
              <a href={zip} download="" className="inline-flex h-8 items-center gap-2 rounded-md px-3 text-xs font-medium text-white accent-gradient">
                <Download className="h-4 w-4" /> Download .zip
              </a>
            )}
            {running && <span className="text-2xs text-fg-4">You can close this dialog — the export continues in the background.</span>}
          </>
        ) : (
          <>
            <span className="text-2xs text-fg-4">
              {prefs.targets.length} target{prefs.targets.length === 1 ? '' : 's'} · {prefs.appearances.length} appearance{prefs.appearances.length === 1 ? '' : 's'} · est. ~{formatSeconds(est)}
            </span>
            <div className="flex-1" />
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" icon={<Download />} loading={submitting} disabled={!prefs.targets.length || !prefs.appearances.length} onClick={() => void start()} data-autofocus>
              Export
            </Button>
          </>
        )
      }
    >
      {job ? (
        <ExportProgress jobState={job.state} progress={job.progress} message={job.message} error={job.error} files={files} />
      ) : (
        <div className="space-y-5">
          <div>
            <Label>Targets</Label>
            <ExportTargetGrid value={prefs.targets} onChange={(targets) => setPrefs((p) => ({ ...p, targets }))} />
          </div>

          <div className="grid grid-cols-[1fr_auto] gap-6">
            <div>
              <Label>Appearances</Label>
              <AppearanceToggles value={prefs.appearances} onChange={(appearances) => setPrefs((p) => ({ ...p, appearances }))} />
              <p className="mt-1.5 text-3xs text-fg-4">Platforms only receive the appearances they support (watchOS: Default only).</p>
            </div>
            <div>
              <Label>Quality</Label>
              <Segmented
                size="md"
                value={prefs.quality}
                onChange={(q) => setPrefs((p) => ({ ...p, quality: q }))}
                options={(['preview', 'final', 'ultra'] as Quality[]).map((q) => ({ value: q, label: presets?.quality[q]?.label ?? q, tip: presets?.quality[q]?.description }))}
              />
              <p className="mt-1.5 max-w-[220px] text-3xs leading-snug text-fg-4">{presets?.quality[prefs.quality]?.description}</p>
            </div>
          </div>
        </div>
      )}
    </Dialog>
  )
}

const toggleIn = <T,>(list: readonly T[], v: T): T[] => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])

/** Export target checkboxes (shared with the Icon Pack page). `dense` = compact two-line tiles. */
export function ExportTargetGrid({
  value,
  onChange,
  dense,
  className,
}: {
  value: readonly ExportTarget[]
  onChange: (targets: ExportTarget[]) => void
  dense?: boolean
  className?: string
}) {
  return (
    <div className={cn('grid gap-2', dense ? 'grid-cols-2 gap-1.5' : 'grid-cols-3', className)}>
      {EXPORT_TARGETS.map((t) => {
        const on = value.includes(t.id)
        return (
          <button
            key={t.id}
            type="button"
            role="checkbox"
            aria-checked={on}
            onClick={() => onChange(toggleIn(value, t.id))}
            data-tip={dense ? t.description : undefined}
            className={cn(
              'group relative flex items-start text-left transition-[border-color,background-color,box-shadow] duration-150',
              dense ? 'items-center gap-2 rounded-lg border px-2 py-1.5' : 'gap-2.5 rounded-xl border p-2.5',
              on ? 'border-accent/55 bg-accent/[0.09] shadow-[0_0_0_1px_rgb(143_125_255/0.25)]' : 'border-line bg-surface-0/40 hover:border-line-2',
            )}
          >
            <span
              className={cn(
                'flex shrink-0 items-center justify-center border transition-colors',
                dense ? 'h-5 w-5 rounded-md' : 'mt-0.5 h-7 w-7 rounded-lg',
                on ? 'border-accent/40 bg-accent/20 text-fg' : 'border-line-2 bg-white/[0.03] text-fg-3',
              )}
            >
              <PlatformIcon platform={t.id} className={dense ? 'h-3 w-3' : 'h-3.5 w-3.5'} />
            </span>
            <span className="min-w-0 flex-1">
              <span className={cn('flex items-center gap-1.5 font-semibold text-fg', dense ? 'text-2xs' : 'text-xs')}>
                <span className="truncate">{t.label}</span>
                {t.badge && <Badge tone="warn">{t.badge}</Badge>}
              </span>
              {!dense && <span className="mt-0.5 block text-3xs leading-snug text-fg-4">{t.description}</span>}
            </span>
            <span
              className={cn(
                'flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border transition-colors',
                !dense && 'absolute right-2 top-2',
                on ? 'border-transparent accent-gradient' : 'border-line-3',
              )}
            >
              {on && <Check className="h-3 w-3 text-white" />}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** Appearance chips — multi-select (Export) or single-select (`single`, Icon Pack render appearance). */
export function AppearanceToggles({
  value,
  onChange,
  single,
  compact,
}: {
  value: readonly AppearanceId[]
  onChange: (appearances: AppearanceId[]) => void
  single?: boolean
  compact?: boolean
}) {
  const presets = useAppStore((s) => s.presets.data)
  return (
    <div className="flex flex-wrap gap-1.5" role={single ? 'radiogroup' : 'group'}>
      {APPEARANCE_IDS.map((a) => {
        const on = value.includes(a)
        const label = appearanceLabel(a, presets)
        return (
          <button
            key={a}
            type="button"
            role={single ? 'radio' : 'checkbox'}
            aria-checked={on}
            aria-label={label}
            data-tip={compact ? label : undefined}
            onClick={() => onChange(single ? [a] : toggleIn(value, a))}
            className={cn(
              'flex items-center gap-2 rounded-lg border text-2xs font-medium transition-colors',
              compact ? 'h-7 p-[3px]' : 'h-8 pl-1 pr-2.5',
              on ? 'border-accent/55 bg-accent/[0.09] text-fg' : 'border-line text-fg-3 hover:border-line-2',
            )}
          >
            <span
              className={cn('flex items-center justify-center rounded-md border border-white/15', compact ? 'h-5 w-5' : 'h-6 w-6')}
              style={{ background: APPEARANCE_VISUAL[a].bg, color: APPEARANCE_VISUAL[a].fg }}
            >
              <AppearanceIcon appearance={a} className="h-3 w-3" />
            </span>
            {!compact && label}
          </button>
        )
      })}
    </div>
  )
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-2 text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">{children}</div>
}

function ExportProgress({
  jobState,
  progress,
  message,
  error,
  files,
}: {
  jobState: string
  progress: number
  message: string
  error?: string | null
  files: { name: string; url: string }[]
}) {
  if (jobState === 'error' || jobState === 'cancelled') {
    return (
      <div className="flex flex-col items-center gap-2 py-8 text-center">
        <CircleAlert className={cn('h-8 w-8', jobState === 'error' ? 'text-bad' : 'text-fg-3')} />
        <div className="text-sm font-semibold text-fg">{jobState === 'error' ? 'Export failed' : 'Export cancelled'}</div>
        {error && <div className="max-w-[520px] break-words text-2xs text-fg-3">{error}</div>}
      </div>
    )
  }
  if (jobState !== 'done') {
    return (
      <div className="flex flex-col items-center gap-4 py-10">
        <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl accent-gradient-soft ring-1 ring-inset ring-white/10">
          <LoaderCircle className="h-7 w-7 animate-spin text-fg" />
        </div>
        <div className="w-full max-w-[420px] space-y-2 text-center">
          <div className="text-xs font-semibold text-fg">{jobState === 'queued' ? 'Waiting for the GPU…' : message || 'Rendering…'}</div>
          <ProgressBar value={jobState === 'running' ? progress : null} />
          <div className="text-3xs tabular text-fg-4">{Math.round(progress * 100)}%</div>
        </div>
      </div>
    )
  }
  return (
    <div>
      <div className="mb-3 flex items-center gap-2 rounded-xl border border-ok/25 bg-ok/[0.07] px-3 py-2.5 text-xs text-fg-2">
        <Check className="h-4 w-4 text-ok" /> Export complete — {files.length} file{files.length === 1 ? '' : 's'}.
      </div>
      <div className="max-h-[340px] overflow-y-auto rounded-xl border border-line">
        {files.map((f) => (
          <a
            key={f.url}
            href={f.url}
            download=""
            className="flex items-center gap-2.5 border-b border-line px-3 py-1.5 text-2xs text-fg-2 last:border-b-0 hover:bg-white/[0.03]"
          >
            {/\.(png|jpg|webp|ico|icns)$/i.test(f.name) ? <FileImage className="h-3.5 w-3.5 text-fg-4" /> : <FileArchive className="h-3.5 w-3.5 text-fg-4" />}
            <span className="min-w-0 flex-1 truncate font-mono">{f.name}</span>
            <Download className="h-3 w-3 text-fg-4" />
          </a>
        ))}
        {!files.length && <div className="px-3 py-6 text-center text-2xs text-fg-4">The server did not list individual files — use the .zip.</div>}
      </div>
    </div>
  )
}
