// Icon Pack (#/pack): pick many icons → one look → render them all (+ optional exports). Results stream in.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Check,
  CircleAlert,
  ClipboardPaste,
  Clock,
  Download,
  FileArchive,
  FolderOpen,
  House,
  ImageDown,
  Layers,
  LayoutGrid,
  ListChecks,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  SquareCheck,
  TriangleAlert,
  X,
} from 'lucide-react'
import type {
  AppearanceId,
  BatchItemResult,
  BatchRequest,
  BatchSource,
  Job,
  ProjectSummary,
  Quality,
  SampleIcon,
  SplitStrategy,
} from '../../types'
import { batchApi, errorMessage, jobsApi, samplesApi } from '../../api'
import { cn, formatElapsed, formatSeconds, relativeTime, titleCase } from '../../lib/format'
import { safeStorage, isTypingTarget, useNow } from '../../lib/hooks'
import { SAMPLE_COLLECTION_LABEL, sampleCollection, STRATEGIES } from '../../lib/meta'
import { useCopiedStyle } from '../../lib/looks'
import { goHome, openProjectRoute } from '../../lib/route'
import { useAppStore } from '../../store/app'
import { toast } from '../../store/toasts'
import { Logo } from '../../components/icons'
import { Badge, Button, EmptyState, IconButton, Kbd, ProgressBar, Segmented, Select, Skeleton, Switch } from '../../components/ui'
import { Backdrop, OfflineNote, SystemPill } from '../home/HomePage'
import { AppearanceToggles, ExportTargetGrid, loadExportPrefs, type ExportPrefs } from '../dialogs/ExportDialog'
import { continueInToast, isActive } from '../dialogs/useJob'
import { LooksGrid } from '../looks/LooksGallery'

// ------------------------------------------------------------------------------------------ model
type SourceKey = string // 's:<sample name>' | 'p:<project id>'
const keyOf = (src: BatchSource): SourceKey => (src.sample != null ? `s:${src.sample}` : `p:${src.projectId ?? ''}`)
const sourceOf = (key: SourceKey): BatchSource => (key.startsWith('s:') ? { sample: key.slice(2) } : { projectId: key.slice(2) })

type StyleChoice = { kind: 'keep' } | { kind: 'look'; id: string } | { kind: 'project'; id: string } | { kind: 'copied' }

interface PackPrefs {
  style: StyleChoice
  quality: 'draft' | 'preview'
  size: number
  appearance: AppearanceId
  strategy: SplitStrategy
  exportOn: boolean
  export: ExportPrefs
}

interface PackRun {
  jobId: string
  startedAt: number
  styleLabel: string
  quality: Quality
  size: number
  exportOn: boolean
  sources: { key: SourceKey; name: string; thumb?: string | null }[]
}

const PREFS_KEY = 'bis.pack.v1'
const RUN_KEY = 'bis.pack.lastRun'
const SELECTION_KEY = 'bis.pack.selection'
// Pack renders are draft/preview only, and the server clamps those tiers to ≤ 512 px (GPU rule), so never offer more.
const SIZES = [256, 512] as const
const MAX_PACK_SIZE = 512

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = safeStorage.get(key)
    return raw ? ({ ...fallback, ...JSON.parse(raw) } as T) : fallback
  } catch {
    return fallback
  }
}

function readPrefs(): PackPrefs {
  const p = readJson(PREFS_KEY, defaultPrefs())
  // Older saved prefs may hold 1024 px (rendered at 512 anyway) or a tier the pack no longer offers.
  const size = (SIZES as readonly number[]).includes(p.size) ? p.size : MAX_PACK_SIZE
  return { ...p, size, quality: p.quality === 'preview' ? 'preview' : 'draft' }
}

function defaultPrefs(): PackPrefs {
  return {
    style: { kind: 'look', id: 'liquid-glass' },
    quality: 'draft',
    size: 512,
    appearance: 'light',
    strategy: 'smart',
    exportOn: false,
    export: { ...loadExportPrefs(), quality: 'preview' },
  }
}

function readRun(): PackRun | null {
  try {
    const raw = safeStorage.get(RUN_KEY)
    const v = raw ? (JSON.parse(raw) as PackRun) : null
    return v && typeof v.jobId === 'string' && Array.isArray(v.sources) ? v : null
  } catch {
    return null
  }
}

function readSelection(): SourceKey[] {
  try {
    const v = JSON.parse(safeStorage.get(SELECTION_KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

function batchItems(job: Job | null): BatchItemResult[] {
  const items = job?.result?.items
  return Array.isArray(items) ? (items as BatchItemResult[]) : []
}

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

const PACK_TOAST = {
  title: 'Rendering icon pack',
  done: 'Icon pack ready',
  doneDescription: (j: Job) => {
    const items = batchItems(j)
    const failed = items.filter((i) => i.error).length
    return `${items.length - failed} icon${items.length - failed === 1 ? '' : 's'} rendered${failed ? ` · ${failed} failed` : ''}`
  },
  links: (j: Job) => {
    const links = []
    const sheet = str(j.result?.contactSheet)
    const zip = str(j.result?.zip)
    if (sheet) links.push({ label: 'Contact sheet', href: sheet, download: true })
    if (zip) links.push({ label: 'Download .zip', href: zip, download: true })
    return links.length ? links : undefined
  },
}

/** Live batch job: WS updates via the app store + a REST poll while it runs (partial `result.items`). */
function useBatchJob(jobId: string | null): { job: Job | null; missing: boolean } {
  const job = useAppStore((s) => (jobId ? (s.jobs[jobId] ?? null) : null))
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    setMissing(false)
    if (!jobId) return
    let stop = false
    let timer: number | undefined
    const tick = async () => {
      try {
        const j = await jobsApi.get(jobId)
        if (stop) return
        useAppStore.getState().upsertJob(j)
        if (!isActive(j)) return
      } catch (e) {
        if (stop) return
        if ((e as { status?: number }).status === 404) {
          setMissing(true)
          return
        }
      }
      // WS already streams the job (partial `result.items` included): poll slowly then, as a safety net only, since
      // a long pack's result grows with every icon.
      timer = window.setTimeout(tick, useAppStore.getState().ws === 'open' ? 5000 : 1500)
    }
    void tick()
    return () => {
      stop = true
      window.clearTimeout(timer)
    }
  }, [jobId])
  return { job, missing }
}

// ------------------------------------------------------------------------------------------ page
export default function PackPage() {
  const loadSamples = useAppStore((s) => s.loadSamples)
  const loadProjects = useAppStore((s) => s.loadProjects)
  const loadPresets = useAppStore((s) => s.loadPresets)
  const samples = useAppStore((s) => s.samples.data)
  const projects = useAppStore((s) => s.projects.data)
  const looks = useAppStore((s) => s.presets.data?.looks)
  const copied = useCopiedStyle()

  const [prefs, setPrefs] = useState<PackPrefs>(readPrefs)
  const [selected, setSelected] = useState<Set<SourceKey>>(() => new Set(readSelection()))
  const [run, setRun] = useState<PackRun | null>(readRun)
  const [view, setView] = useState<'sources' | 'results'>(() => (readRun() ? 'results' : 'sources'))
  const [submitting, setSubmitting] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const { job, missing } = useBatchJob(run?.jobId ?? null)

  useEffect(() => {
    void loadSamples()
    void loadProjects()
    void loadPresets()
  }, [loadSamples, loadProjects, loadPresets])
  useEffect(() => safeStorage.set(PREFS_KEY, JSON.stringify(prefs)), [prefs])
  useEffect(() => safeStorage.set(SELECTION_KEY, JSON.stringify([...selected])), [selected])
  useEffect(() => {
    if (run) safeStorage.set(RUN_KEY, JSON.stringify(run))
    else safeStorage.remove(RUN_KEY)
  }, [run])
  useEffect(() => {
    document.title = 'Icon Pack · Blender Icon Studio'
  }, [])

  // The server forgot the job (restart): drop the stale run.
  useEffect(() => {
    if (missing) {
      setRun(null)
      setView('sources')
    }
  }, [missing])

  // Finished: refresh the project list (new projects were created) once per job.
  const reported = useRef<string | null>(null)
  useEffect(() => {
    if (!job || isActive(job) || reported.current === job.id) return
    reported.current = job.id
    void loadProjects()
  }, [job, loadProjects])

  // Leaving the page while the pack renders: keep reporting it in a toast. (Deferred so React StrictMode's
  // simulated unmount/remount does not count as leaving.)
  const jobRef = useRef(job)
  jobRef.current = job
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      window.setTimeout(() => {
        if (!mounted.current) continueInToast(jobRef.current, PACK_TOAST)
      }, 0)
    }
  }, [])

  const sampleMap = useMemo(() => new Map((samples ?? []).map((s) => [s.name, s])), [samples])
  const projectMap = useMemo(() => new Map((projects ?? []).map((p) => [p.id, p])), [projects])
  const describe = (key: SourceKey): { name: string; thumb: string | null } => {
    if (key.startsWith('s:')) return { name: key.slice(2), thumb: samplesApi.thumbnailUrl(key.slice(2)) }
    const p = projectMap.get(key.slice(2))
    return { name: p?.name ?? 'Project', thumb: p?.thumbnail ?? null }
  }
  // Selection entries that no longer exist (deleted project / renamed sample) are ignored.
  const validSelection = useMemo(
    () => [...selected].filter((k) => (k.startsWith('s:') ? !samples || sampleMap.has(k.slice(2)) : !projects || projectMap.has(k.slice(2)))),
    [selected, samples, projects, sampleMap, projectMap],
  )

  const running = isActive(job)
  const start = async () => {
    if (!validSelection.length || running) return
    const req: BatchRequest = {
      sources: validSelection.map(sourceOf),
      strategy: prefs.strategy,
      quality: prefs.quality,
      size: prefs.size,
      appearance: prefs.appearance,
      export: prefs.exportOn && prefs.export.targets.length ? { ...prefs.export, appearances: prefs.export.appearances.length ? prefs.export.appearances : ['light'] } : null,
    }
    const st = prefs.style
    let styleLabel = 'Each icon’s own style'
    if (st.kind === 'look') {
      req.look = st.id
      styleLabel = looks?.[st.id]?.label ?? st.id
    } else if (st.kind === 'project') {
      req.fromProject = st.id
      styleLabel = `Style of “${projectMap.get(st.id)?.name ?? 'project'}”`
    } else if (st.kind === 'copied') {
      if (!copied) return
      req.style = copied.style
      styleLabel = `Copied style (“${copied.sourceName}”)`
    }
    setSubmitting(true)
    try {
      const j = await batchApi.start(req)
      useAppStore.getState().upsertJob(j)
      setRun({
        jobId: j.id,
        startedAt: Date.now(),
        styleLabel,
        quality: prefs.quality,
        size: prefs.size,
        exportOn: !!req.export,
        sources: validSelection.map((key) => ({ key, ...describe(key) })),
      })
      setView('results')
      scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
    } catch (e) {
      toast.error('Icon Pack could not start', { description: errorMessage(e) })
    } finally {
      setSubmitting(false)
    }
  }

  const styleValid =
    prefs.style.kind === 'keep' ||
    (prefs.style.kind === 'look' && !!looks?.[prefs.style.id]) ||
    (prefs.style.kind === 'project' && projectMap.has(prefs.style.id)) ||
    (prefs.style.kind === 'copied' && !!copied)

  return (
    <div ref={scrollRef} className="relative h-full overflow-y-auto overflow-x-hidden bg-app">
      <Backdrop />
      <PackNav />
      <main className="relative mx-auto max-w-[1320px] px-8 pb-24">
        <Intro />
        <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_392px]">
          <section className="min-w-0 rounded-2xl border border-line bg-surface-1/70 backdrop-blur-sm">
            <div className="flex h-12 items-center gap-3 border-b border-line px-4">
              <Segmented
                size="md"
                value={view}
                onChange={setView}
                ariaLabel="Icon Pack view"
                options={[
                  { value: 'sources', icon: <ListChecks />, label: `Icons${validSelection.length ? ` · ${validSelection.length}` : ''}` },
                  {
                    value: 'results',
                    icon: running ? <RefreshCw className="animate-spin" /> : <LayoutGrid />,
                    label: 'Results',
                    disabled: !run,
                    tip: run ? undefined : 'Run a pack to see results here',
                  },
                ]}
              />
              <div className="flex-1" />
              {view === 'sources' && validSelection.length > 0 && (
                <span className="flex items-center gap-2 text-2xs text-fg-3">
                  <span className="tabular">{validSelection.length} selected</span>
                  <Button size="xs" variant="ghost" icon={<X />} onClick={() => setSelected(new Set())}>
                    Clear
                  </Button>
                </span>
              )}
            </div>
            {view === 'sources' ? (
              <SourcePicker selected={selected} setSelected={setSelected} />
            ) : run ? (
              <Results key={run.jobId} run={run} job={job} onNew={() => setView('sources')} onClear={() => (setRun(null), setView('sources'))} />
            ) : null}
          </section>

          <aside className="lg:sticky lg:top-[64px]">
            <Settings
              prefs={prefs}
              setPrefs={setPrefs}
              projects={projects}
              count={validSelection.length}
              projectCount={validSelection.filter((k) => k.startsWith('p:')).length}
              running={running}
              submitting={submitting}
              disabled={!validSelection.length || !styleValid}
              onRun={() => void start()}
              onShowResults={run ? () => setView('results') : undefined}
            />
          </aside>
        </div>
      </main>
    </div>
  )
}

function PackNav() {
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-app/60 backdrop-blur-xl">
      <div className="mx-auto flex h-12 max-w-[1320px] items-center gap-3 px-8">
        <button type="button" onClick={goHome} className="flex items-center gap-2.5 rounded-md py-1 pr-1.5 transition-opacity hover:opacity-85" aria-label="Back to home">
          <Logo size={22} />
          <span className="text-[13px] font-semibold tracking-[-0.01em] text-fg">Blender Icon Studio</span>
        </button>
        <span className="text-fg-4">/</span>
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-fg">
          <LayoutGrid className="h-3.5 w-3.5 text-accent" /> Icon Pack
        </span>
        <div className="flex-1" />
        <SystemPill />
        <Button variant="ghost" size="sm" icon={<House />} onClick={goHome}>
          Home
        </Button>
      </div>
    </header>
  )
}

function Intro() {
  const steps = [
    ['1', 'Pick icons', 'Samples or your own projects'],
    ['2', 'Choose one look', 'Applied to every icon'],
    ['3', 'Render the pack', 'Contact sheet + zip'],
  ]
  return (
    <section className="flex flex-wrap items-end justify-between gap-6 pb-6 pt-9 animate-slide-up">
      <div>
        <h1 className="font-display text-[34px] font-semibold leading-tight tracking-[-0.03em] text-fg">
          Icon Pack: <span className="text-gradient">one look, every icon.</span>
        </h1>
        <p className="mt-2 max-w-[620px] text-[13.5px] leading-relaxed text-fg-3">
          Got a whole icon set? Select the icons, pick a look and Blender renders them all on your GPU, and each one becomes a project you can fine-tune later.
        </p>
      </div>
      <ol className="flex gap-2">
        {steps.map(([n, title, sub]) => (
          <li key={n} className="flex items-center gap-2.5 rounded-xl border border-line bg-surface-1/60 py-2 pl-2 pr-3.5 backdrop-blur">
            <span className="flex h-6 w-6 items-center justify-center rounded-full accent-gradient-soft text-2xs font-semibold text-fg ring-1 ring-inset ring-white/10">{n}</span>
            <span>
              <span className="block text-2xs font-semibold text-fg-2">{title}</span>
              <span className="block text-3xs text-fg-4">{sub}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}

// ------------------------------------------------------------------------------------------ sources
function SourcePicker({ selected, setSelected }: { selected: Set<SourceKey>; setSelected: (s: Set<SourceKey>) => void }) {
  const { data: samples, loading: samplesLoading, error: samplesError } = useAppStore((s) => s.samples)
  const { data: projects, error: projectsError } = useAppStore((s) => s.projects)
  const loadSamples = useAppStore((s) => s.loadSamples)
  const loadProjects = useAppStore((s) => s.loadProjects)
  const [tab, setTab] = useState<'samples' | 'projects'>('samples')
  const [query, setQuery] = useState('')
  const [collection, setCollection] = useState('corpus')
  const searchRef = useRef<HTMLInputElement>(null)
  const anchor = useRef<number | null>(null)

  const collections = useMemo(() => {
    const counts = new Map<string, number>()
    for (const s of samples ?? []) counts.set(sampleCollection(s), (counts.get(sampleCollection(s)) ?? 0) + 1)
    return new Map([...counts].sort(([a], [b]) => (a === 'corpus' ? -1 : b === 'corpus' ? 1 : a.localeCompare(b))))
  }, [samples])
  const activeCollection = collections.has(collection) ? collection : 'all'

  const q = query.trim().toLowerCase()
  const sampleList = useMemo(() => {
    const inCol = activeCollection === 'all' ? (samples ?? []) : (samples ?? []).filter((s) => sampleCollection(s) === activeCollection)
    return q ? inCol.filter((s) => s.name.toLowerCase().includes(q) || s.file.toLowerCase().includes(q)) : inCol
  }, [samples, activeCollection, q])
  const projectList = useMemo(() => (q ? (projects ?? []).filter((p) => p.name.toLowerCase().includes(q)) : (projects ?? [])), [projects, q])
  const keys = useMemo(
    () => (tab === 'samples' ? sampleList.map((s) => `s:${s.name}`) : projectList.map((p) => `p:${p.id}`)),
    [tab, sampleList, projectList],
  )
  const selectedHere = keys.filter((k) => selected.has(k)).length

  useEffect(() => {
    anchor.current = null
  }, [tab, q, activeCollection])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target) || document.querySelector('[data-dialog]')) return
      if (e.key === '/') {
        e.preventDefault()
        searchRef.current?.focus()
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
        e.preventDefault()
        setSelected(new Set([...selected, ...keys]))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [keys, selected, setSelected])

  const toggle = (index: number, shift: boolean) => {
    const key = keys[index]
    const next = new Set(selected)
    if (shift && anchor.current != null && keys[anchor.current]) {
      // Range: give every tile between the anchor and here the anchor's state.
      const on = selected.has(keys[anchor.current])
      const [lo, hi] = anchor.current < index ? [anchor.current, index] : [index, anchor.current]
      for (let i = lo; i <= hi; i++) on ? next.add(keys[i]) : next.delete(keys[i])
    } else {
      if (next.has(key)) next.delete(key)
      else next.add(key)
      anchor.current = index
    }
    setSelected(next)
  }
  const selectAll = () => setSelected(new Set([...selected, ...keys]))
  const selectNone = () => setSelected(new Set([...selected].filter((k) => !keys.includes(k))))

  return (
    <div className="p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented
          size="sm"
          value={tab}
          onChange={setTab}
          ariaLabel="Source"
          options={[
            { value: 'samples', icon: <FolderOpen />, label: `Samples${samples ? ` · ${samples.length}` : ''}` },
            { value: 'projects', icon: <Layers />, label: `Your projects${projects ? ` · ${projects.length}` : ''}` },
          ]}
        />
        {tab === 'samples' && collections.size > 1 && (
          <Segmented
            size="sm"
            value={activeCollection}
            onChange={setCollection}
            ariaLabel="Collection"
            options={[
              ...[...collections.entries()].map(([c, n]) => ({ value: c, label: `${SAMPLE_COLLECTION_LABEL[c] ?? titleCase(c)} · ${n}` })),
              { value: 'all', label: 'All' },
            ]}
          />
        )}
        <div className="flex-1" />
        <label className="relative flex h-7 w-[220px] items-center rounded-lg border border-line bg-surface-0/80 pl-7 pr-2 transition-colors focus-within:border-accent/60">
          <Search className="absolute left-2 h-3.5 w-3.5 text-fg-4" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Escape' && setQuery('')}
            placeholder={tab === 'samples' ? 'Search samples' : 'Search projects'}
            aria-label="Search"
            className="h-full w-full bg-transparent text-xs text-fg outline-none placeholder:text-fg-4"
          />
          {query ? (
            <button type="button" aria-label="Clear search" onClick={() => setQuery('')} className="text-fg-4 hover:text-fg">
              <X className="h-3.5 w-3.5" />
            </button>
          ) : (
            <Kbd>/</Kbd>
          )}
        </label>
      </div>
      <div className="mb-3 flex items-center gap-2 text-2xs text-fg-3">
        <Button size="xs" variant="subtle" icon={<SquareCheck />} onClick={selectAll} disabled={!keys.length || selectedHere === keys.length} tipLabel="Select every icon shown" tipKbd="Mod+A">
          Select all{keys.length ? ` ${keys.length}` : ''}
        </Button>
        <Button size="xs" variant="subtle" icon={<Square />} onClick={selectNone} disabled={!selectedHere}>
          Select none
        </Button>
        <span className="ml-1 text-fg-4">
          {selectedHere ? `${selectedHere} of ${keys.length} shown selected` : 'Click to select · Shift-click selects a range'}
        </span>
      </div>

      {tab === 'samples' ? (
        samplesError && !samples ? (
          <OfflineNote error={samplesError} onRetry={() => void loadSamples(true)} />
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-2">
            {!samples &&
              samplesLoading !== false &&
              Array.from({ length: 18 }).map((_, i) => (
                <div key={i} className="rounded-xl border border-line bg-surface-1 p-2.5">
                  <Skeleton className="mx-auto aspect-square w-[74%] rounded-[22%]" />
                  <Skeleton className="mx-auto mt-2.5 h-2.5 w-1/2" />
                </div>
              ))}
            {sampleList.map((s, i) => (
              <SourceTile
                key={s.name}
                name={s.name}
                thumb={samplesApi.thumbnailUrl(s.name)}
                selected={selected.has(`s:${s.name}`)}
                index={i}
                onToggle={(shift) => toggle(i, shift)}
              />
            ))}
          </div>
        )
      ) : projectsError && !projects ? (
        <OfflineNote error={projectsError} onRetry={() => void loadProjects()} />
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-2">
          {projectList.map((p, i) => (
            <SourceTile
              key={p.id}
              name={p.name}
              sub={relativeTime(p.updatedAt)}
              thumb={p.thumbnail ? `${p.thumbnail}${p.thumbnail.includes('?') ? '&' : '?'}t=${encodeURIComponent(p.updatedAt)}` : null}
              selected={selected.has(`p:${p.id}`)}
              index={i}
              onToggle={(shift) => toggle(i, shift)}
            />
          ))}
        </div>
      )}
      {tab === 'samples' && samples && sampleList.length === 0 && (
        <EmptyState icon={<Search />} title={`No samples match “${query}”`}>
          Try another name or switch the collection.
        </EmptyState>
      )}
      {tab === 'projects' && projects && projectList.length === 0 && (
        <EmptyState icon={<Layers />} title={query ? `No projects match “${query}”` : 'No projects yet'}>
          {query ? 'Try another name.' : 'Import an SVG or open a sample on the home screen. Your projects show up here.'}
        </EmptyState>
      )}
    </div>
  )
}

function SourceTile({
  name,
  sub,
  thumb,
  selected,
  index,
  onToggle,
}: {
  name: string
  sub?: string
  thumb: string | null
  selected: boolean
  index: number
  onToggle: (shift: boolean) => void
}) {
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={selected}
      aria-label={name}
      onClick={(e) => onToggle(e.shiftKey)}
      className={cn(
        'group relative flex flex-col items-center rounded-xl border px-2 pb-2 pt-2.5 transition-[border-color,background-color,box-shadow] duration-150 animate-fade-in',
        selected
          ? 'border-accent/60 bg-accent/[0.08] shadow-[0_0_0_1px_rgb(143_125_255/0.3)]'
          : 'border-line bg-surface-1/60 hover:border-line-3 hover:bg-surface-2',
      )}
      style={{ animationDelay: `${Math.min(index, 40) * 10}ms` }}
    >
      <span
        className={cn(
          'absolute left-1.5 top-1.5 z-[1] flex h-4 w-4 items-center justify-center rounded-[5px] border transition-[background-color,border-color,opacity]',
          selected ? 'border-transparent accent-gradient' : 'border-line-3 bg-surface-0/80 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100',
        )}
      >
        {selected && <Check className="h-3 w-3 text-white" strokeWidth={3} />}
      </span>
      <span className="relative aspect-square w-[74%]">
        {thumb && !failed ? (
          <>
            {!loaded && <Skeleton className="absolute inset-0 rounded-[22%]" />}
            <img
              src={thumb}
              alt=""
              loading="lazy"
              decoding="async"
              draggable={false}
              onLoad={() => setLoaded(true)}
              onError={() => setFailed(true)}
              className={cn(
                'absolute inset-0 h-full w-full object-contain drop-shadow-[0_6px_10px_rgb(0_0_0/0.45)] transition-[opacity,transform] duration-200',
                loaded ? 'opacity-100' : 'opacity-0',
                selected ? 'scale-[0.94]' : 'group-hover:scale-[1.04]',
              )}
            />
          </>
        ) : (
          <span className="absolute inset-0 flex items-center justify-center rounded-[22%] bg-surface-3 text-fg-4">
            <Layers className="h-5 w-5" />
          </span>
        )}
      </span>
      <span className={cn('mt-2 w-full truncate text-center text-3xs font-medium', selected ? 'text-fg' : 'text-fg-3 group-hover:text-fg-2')}>{name}</span>
      {sub && <span className="w-full truncate text-center text-[9.5px] text-fg-4">{sub}</span>}
    </button>
  )
}

// ------------------------------------------------------------------------------------------ settings
function Settings({
  prefs,
  setPrefs,
  projects,
  count,
  projectCount,
  running,
  submitting,
  disabled,
  onRun,
  onShowResults,
}: {
  prefs: PackPrefs
  setPrefs: React.Dispatch<React.SetStateAction<PackPrefs>>
  projects: ProjectSummary[] | null
  count: number
  projectCount: number
  running: boolean
  submitting: boolean
  disabled: boolean
  onRun: () => void
  onShowResults?: () => void
}) {
  const presets = useAppStore((s) => s.presets.data)
  const copied = useCopiedStyle()
  const [hover, setHover] = useState<string | null>(null)
  const set = (patch: Partial<PackPrefs>) => setPrefs((p) => ({ ...p, ...patch }))
  const st = prefs.style
  const lookId = st.kind === 'look' ? st.id : null
  const shownLook = presets?.looks?.[hover ?? lookId ?? ''] ?? null

  const styleInfo: { title: string; body: string } = shownLook
    ? { title: shownLook.label, body: shownLook.description }
    : st.kind === 'keep'
      ? { title: 'Keep each icon’s style', body: 'Samples get the default stack; your projects keep their own materials and lighting.' }
      : st.kind === 'copied'
        ? { title: 'Copied style', body: copied ? `The style you copied from “${copied.sourceName}”.` : 'Nothing copied. Copy a style in the editor (Ctrl+Alt+C).' }
        : st.kind === 'project'
          ? { title: 'Style of a project', body: `Materials, depth, plate and lighting of “${projects?.find((p) => p.id === st.id)?.name ?? '…'}”.` }
          : { title: 'Pick a look', body: '' }

  const perIcon = (prefs.quality === 'preview' ? 4 : 1.2) * (prefs.size >= 512 ? 1 : 0.6) +(prefs.exportOn ? (prefs.export.quality === 'final' ? 10 : prefs.export.quality === 'ultra' ? 45 : 2.5) * Math.max(1, prefs.export.appearances.length) : 0)
  const est = count * perIcon

  return (
    <div className="flex max-h-none flex-col overflow-hidden rounded-2xl border border-line bg-surface-1/80 backdrop-blur-sm lg:max-h-[calc(100vh-88px)]">
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4">
        <Group title="Look" hint="Applied to every icon">
          <div className="mb-2 min-h-[46px] rounded-lg border border-line bg-surface-0/50 px-2.5 py-2">
            <div className="text-2xs font-semibold text-fg-2">{styleInfo.title}</div>
            {styleInfo.body && <div className="mt-0.5 text-3xs leading-snug text-fg-4">{styleInfo.body}</div>}
          </div>
          <LooksGrid
            presets={presets}
            value={lookId}
            onPick={(id) => set({ style: { kind: 'look', id } })}
            onHover={setHover}
            variant="compact"
            columns={4}
            before={
              <>
                <OptionTile
                  active={st.kind === 'keep'}
                  label="Keep style"
                  tip="Keep each icon’s own style"
                  icon={<Layers />}
                  onClick={() => set({ style: { kind: 'keep' } })}
                />
                {copied && (
                  <OptionTile
                    active={st.kind === 'copied'}
                    label="Copied"
                    tip={`Use the style copied from “${copied.sourceName}”`}
                    icon={<ClipboardPaste />}
                    onClick={() => set({ style: { kind: 'copied' } })}
                  />
                )}
              </>
            }
          />
          <div className="mt-2.5 flex items-center gap-2">
            <span className="shrink-0 text-2xs text-fg-3">Copy style from</span>
            <Select
              value={st.kind === 'project' ? st.id : null}
              placeholder={projects?.length ? 'a project…' : 'no projects yet'}
              disabled={!projects?.length}
              onChange={(id) => set({ style: { kind: 'project', id } })}
              options={(projects ?? []).map((p) => ({ value: p.id, label: p.name, description: `${p.layerCount} layers · ${relativeTime(p.updatedAt)}` }))}
              className={cn('min-w-0 flex-1', st.kind === 'project' && 'border-accent/60 bg-accent/[0.08]')}
              menuWidth={300}
              ariaLabel="Copy style from project"
            />
          </div>
        </Group>

        <Group title="Render">
          <Field label="Quality">
            <Segmented
              size="sm"
              fill
              value={prefs.quality}
              onChange={(quality) => set({ quality })}
              options={(['draft', 'preview'] as const).map((q) => ({
                value: q,
                label: `${presets?.quality[q]?.label ?? titleCase(q)} · ${q === 'draft' ? 'EEVEE' : 'Cycles'}`,
                tip: presets?.quality[q]?.description,
              }))}
            />
          </Field>
          <Field label="Size">
            <Segmented size="sm" fill value={String(prefs.size)} onChange={(v) => set({ size: Number(v) })} options={SIZES.map((s) => ({ value: String(s), label: `${s} px` }))} />
          </Field>
          <Field label="Appearance">
            <AppearanceToggles single compact value={[prefs.appearance]} onChange={([a]) => a && set({ appearance: a })} />
          </Field>
          <Field label="Split">
            <Select
              value={prefs.strategy}
              onChange={(strategy) => set({ strategy })}
              options={STRATEGIES.map((s) => ({ value: s.id, label: s.label, description: s.description }))}
              className="flex-1"
              menuWidth={280}
              ariaLabel="Split strategy for new imports"
            />
          </Field>
        </Group>

        <Group
          title="Export"
          hint="Optional"
          right={<Switch checked={prefs.exportOn} onChange={(exportOn) => set({ exportOn })} label="Also export every icon" />}
        >
          {prefs.exportOn ? (
            <div className="space-y-3 animate-fade-in">
              <ExportTargetGrid dense value={prefs.export.targets} onChange={(targets) => set({ export: { ...prefs.export, targets } })} />
              <Field label="Appearances">
                <AppearanceToggles compact value={prefs.export.appearances} onChange={(appearances) => set({ export: { ...prefs.export, appearances } })} />
              </Field>
              <Field label="Quality">
                <Segmented
                  size="sm"
                  fill
                  value={prefs.export.quality}
                  onChange={(quality) => set({ export: { ...prefs.export, quality } })}
                  options={(['preview', 'final'] as Quality[]).map((q) => ({ value: q, label: presets?.quality[q]?.label ?? titleCase(q), tip: presets?.quality[q]?.description }))}
                />
              </Field>
              {!prefs.export.targets.length && <p className="text-3xs text-warn">Pick at least one target, or switch exporting off.</p>}
            </div>
          ) : (
            <p className="text-3xs leading-snug text-fg-4">Switch on to package every icon for iOS, macOS, Android, Windows or the web into one zip.</p>
          )}
        </Group>
      </div>

      <div className="space-y-2.5 border-t border-line bg-surface-0/60 p-4">
        {projectCount > 0 && prefs.style.kind !== 'keep' && (
          <div className="flex items-start gap-1.5 rounded-md border border-warn/20 bg-warn/[0.07] px-2 py-1.5 text-3xs leading-snug text-warn/90">
            <TriangleAlert className="mt-px h-3 w-3 shrink-0" />
            <span>
              {projectCount} existing project{projectCount === 1 ? ' is' : 's are'} restyled in place. Duplicate {projectCount === 1 ? 'it' : 'them'} first to keep the original.
            </span>
          </div>
        )}
        <div className="flex items-center justify-between text-2xs text-fg-3">
          <span>
            <b className="font-semibold tabular text-fg-2">{count}</b> icon{count === 1 ? '' : 's'} · {prefs.size} px · {prefs.quality}
          </span>
          {count > 0 && (
            <span className="flex items-center gap-1 text-fg-4" data-tip="Rough estimate on the RTX 3070 Ti">
              <Clock className="h-3 w-3" /> ≈ {formatSeconds(est)}
            </span>
          )}
        </div>
        {running ? (
          <Button variant="secondary" size="lg" className="w-full" icon={<LayoutGrid />} onClick={onShowResults}>
            A pack is rendering: show results
          </Button>
        ) : (
          <Button variant="primary" size="lg" className="w-full" icon={<Play className="fill-current" />} loading={submitting} disabled={disabled} onClick={onRun}>
            {count ? `Render ${count} icon${count === 1 ? '' : 's'}` : 'Select icons to render'}
          </Button>
        )}
      </div>
    </div>
  )
}

function Group({ title, hint, right, children }: { title: string; hint?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-3xs font-semibold uppercase tracking-[0.09em] text-fg-3">{title}</span>
        {hint && <span className="text-3xs text-fg-4">{hint}</span>}
        <div className="flex-1" />
        {right}
      </div>
      <div className="space-y-2">{children}</div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-h-7 items-center gap-3">
      <span className="w-[76px] shrink-0 text-2xs text-fg-3">{label}</span>
      <div className="flex min-w-0 flex-1 items-center">{children}</div>
    </div>
  )
}

function OptionTile({ active, label, tip, icon, onClick }: { active: boolean; label: string; tip: string; icon: ReactNode; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={active} aria-label={tip} data-tip={tip} className="group flex min-w-0 flex-col items-center gap-1 outline-none">
      <span
        className={cn(
          'relative flex aspect-square w-full items-center justify-center rounded-[10px] border bg-[radial-gradient(circle_at_30%_25%,rgb(255_255_255/0.08),transparent_60%),linear-gradient(160deg,#24242e,#15151b)] text-fg-3 transition-[box-shadow,transform,border-color] duration-150 [&>svg]:h-5 [&>svg]:w-5',
          active
            ? 'border-transparent text-fg shadow-[0_0_0_2px_var(--color-accent)]'
            : 'border-line-2 group-hover:-translate-y-px group-hover:border-line-3 group-hover:text-fg-2 group-focus-visible:shadow-[0_0_0_2px_color-mix(in_srgb,var(--color-accent)_70%,transparent)]',
        )}
      >
        {icon}
      </span>
      <span className={cn('w-full truncate text-center text-[10px] leading-tight', active ? 'font-medium text-fg' : 'text-fg-3 group-hover:text-fg-2')}>{label}</span>
    </button>
  )
}

// ------------------------------------------------------------------------------------------ results
function Results({ run, job, onNew, onClear }: { run: PackRun; job: Job | null; onNew: () => void; onClear: () => void }) {
  const active = isActive(job)
  const now = useNow(active, 500)
  const items = batchItems(job)
  const byKey = useMemo(() => new Map(items.map((it) => [keyOf(it.source), it])), [items])
  const done = run.sources.filter((s) => byKey.has(s.key)).length
  const rendered = items.filter((i) => i.renderUrl).length
  const failed = items.filter((i) => !i.renderUrl).length
  const exportFailed = items.filter((i) => i.renderUrl && i.error).length
  const sheet = str(job?.result?.contactSheet)
  const zip = str(job?.result?.zip)
  // The server reports "Icon i/n: name" while it works; items arrive with the result (or earlier when the
  // server publishes partial `result.items`).
  const m = active ? /Icon\s+(\d+)\s*\/\s*(\d+)/.exec(job?.message ?? '') : null
  const current = m ? Math.max(0, Number(m[1]) - 1) : run.sources.findIndex((s) => !byKey.has(s.key))
  // Highest "Icon i/n" seen: a cancelled job reports no items, but the icons before it were rendered.
  const seen = useRef(0)
  const passedNow = m ? current : done
  if (passedNow > seen.current) seen.current = passedNow
  const passed = active ? passedNow : Math.max(done, seen.current)
  const started = job?.startedAt ? Date.parse(job.startedAt) : run.startedAt
  const finished = job?.finishedAt ? Date.parse(job.finishedAt) : null
  const elapsed = (finished ?? now) - started
  const state = job?.state ?? 'queued'
  // The size the server actually rendered at (it clamps draft/preview), else what was asked for.
  const size = typeof job?.result?.size === 'number' ? job.result.size : run.size

  return (
    <div className="p-4">
      <div className="mb-4 rounded-xl border border-line bg-surface-0/50 p-3.5">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-xs font-semibold text-fg">
              {state === 'done' ? (
                <Check className="h-4 w-4 text-ok" />
              ) : state === 'error' ? (
                <CircleAlert className="h-4 w-4 text-bad" />
              ) : state === 'cancelled' ? (
                <X className="h-4 w-4 text-fg-3" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5 animate-spin text-accent-2" />
              )}
              {state === 'done'
                ? `Pack ready: ${rendered} of ${run.sources.length} icon${run.sources.length === 1 ? '' : 's'} rendered`
                : state === 'error'
                  ? 'The pack stopped with an error'
                  : state === 'cancelled'
                    ? `Cancelled after ${passed} of ${run.sources.length} icon${run.sources.length === 1 ? '' : 's'}`
                    : state === 'queued'
                      ? 'Waiting for the GPU…'
                      : `Rendering ${Math.min(passed + 1, run.sources.length)} of ${run.sources.length}…`}
            </div>
            <div className="mt-0.5 truncate text-3xs text-fg-4">
              {run.styleLabel} · {titleCase(run.quality)} {Math.min(size, MAX_PACK_SIZE)} px{run.exportOn ? ' · with exports' : ''} · {formatElapsed(Math.max(0, elapsed))}
              {failed ? ` · ${failed} failed` : ''}
              {exportFailed ? ` · ${exportFailed} export${exportFailed === 1 ? '' : 's'} failed` : ''}
              {state === 'cancelled' && passed > done ? ' · finished icons are in Your projects' : ''}
              {active && job?.message ? ` · ${job.message}` : ''}
            </div>
          </div>
          {active && job && (
            <Button size="sm" variant="ghost" icon={<X />} onClick={() => void jobsApi.cancel(job.id).catch((e) => toast.error('Cancel failed', { description: errorMessage(e) }))}>
              Cancel
            </Button>
          )}
          {sheet && (
            <a href={sheet} download="" className="inline-flex h-7 items-center gap-1.5 rounded-md border border-line-2 bg-surface-3 px-2.5 text-xs font-medium text-fg transition-colors hover:bg-surface-4">
              <ImageDown className="h-3.5 w-3.5" /> Contact sheet
            </a>
          )}
          {zip && (
            <a href={zip} download="" className="inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium text-white accent-gradient shadow-[0_6px_18px_-6px_rgb(123_102_255/0.7)] hover:brightness-110">
              <FileArchive className="h-3.5 w-3.5" /> Download .zip
            </a>
          )}
          {!active && (
            <>
              <Button size="sm" variant="secondary" icon={<RotateCcw />} onClick={onNew}>
                New pack
              </Button>
              <IconButton label="Clear results" onClick={onClear}>
                <X />
              </IconButton>
            </>
          )}
        </div>
        <ProgressBar
          value={
            active
              ? state === 'running'
                ? Math.max(job?.progress ?? 0, run.sources.length ? passed / run.sources.length : 0)
                : null
              : state === 'done'
                ? 1
                : run.sources.length
                  ? passed / run.sources.length
                  : 0
          }
          className={cn('mt-3', state === 'error' && '[&>div]:!bg-bad [&>div]:!bg-none', state === 'cancelled' && 'opacity-40')}
        />
        {state === 'error' && job?.error && <div className="mt-2 break-words text-2xs text-bad/90">{job.error}</div>}
      </div>

      {sheet && state === 'done' && (
        <a href={sheet} target="_blank" rel="noreferrer" className="group mb-4 block overflow-hidden rounded-xl border border-line checkerboard" data-tip="Open the contact sheet">
          <img src={sheet} alt="Contact sheet" className="mx-auto max-h-[260px] object-contain transition-transform duration-300 group-hover:scale-[1.01]" />
        </a>
      )}

      <div className="grid grid-cols-[repeat(auto-fill,minmax(132px,1fr))] gap-2.5">
        {run.sources.map((s, i) => (
          <ResultTile
            key={s.key}
            source={s}
            jobId={run.jobId}
            item={byKey.get(s.key) ?? null}
            pending={active ? (i < passed ? 'rendered' : state === 'running' && i === current ? 'rendering' : 'queued') : i < passed ? 'rendered' : 'skipped'}
            index={i}
          />
        ))}
      </div>
    </div>
  )
}

function ResultTile({
  source,
  jobId,
  item,
  pending,
  index,
}: {
  source: PackRun['sources'][number]
  jobId: string
  item: BatchItemResult | null
  pending: 'rendered' | 'rendering' | 'queued' | 'skipped'
  index: number
}) {
  const [loaded, setLoaded] = useState(false)
  const [guessFailed, setGuessFailed] = useState(false)
  // Existing projects render to a known path, so their image can show before the batch result arrives.
  const projectId = item?.projectId ?? (source.key.startsWith('p:') ? source.key.slice(2) : null)
  const guess = !item && pending === 'rendered' && projectId && !guessFailed ? `/files/projects/${encodeURIComponent(projectId)}/renders/${encodeURIComponent(jobId)}.png` : null
  const openable = !!item?.projectId || (pending === 'rendered' && !!projectId)
  const name = item?.name || source.name
  const body = item ? (
    item.error && !item.renderUrl ? (
      <span className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 p-3 text-center">
        <CircleAlert className="h-5 w-5 text-bad" />
        <span className="line-clamp-3 max-w-full text-3xs leading-snug text-bad/90 [overflow-wrap:anywhere]" data-tip={item.error}>
          {item.error}
        </span>
      </span>
    ) : item.renderUrl ? (
      <>
        {!loaded && <Skeleton className="absolute inset-[12%] rounded-[22%]" />}
        <img
          src={item.renderUrl}
          alt=""
          draggable={false}
          onLoad={() => setLoaded(true)}
          className={cn('absolute inset-[6%] h-[88%] w-[88%] object-contain transition-[opacity,transform] duration-300 group-hover:scale-[1.03]', loaded ? 'opacity-100' : 'opacity-0')}
          style={loaded ? { animation: 'crossfade-in 380ms ease-out both' } : undefined}
        />
      </>
    ) : (
      <span className="absolute inset-0 flex items-center justify-center text-3xs text-fg-4">No render</span>
    )
  ) : guess ? (
    <img src={guess} alt="" draggable={false} onError={() => setGuessFailed(true)} className="absolute inset-[6%] h-[88%] w-[88%] object-contain" style={{ animation: 'crossfade-in 380ms ease-out both' }} />
  ) : (
    <>
      {source.thumb && (
        <img
          src={source.thumb}
          alt=""
          draggable={false}
          className={cn(
            'absolute inset-[18%] h-[64%] w-[64%] object-contain transition-opacity',
            pending === 'rendered' ? 'opacity-70' : pending === 'rendering' ? 'opacity-40 grayscale' : 'opacity-25 grayscale',
          )}
        />
      )}
      {pending === 'rendered' ? (
        <span className="absolute bottom-1.5 right-1.5 flex items-center gap-0.5 rounded bg-black/45 px-1 text-[9px] font-medium text-ok" data-tip="Rendered: the project is already in Your projects">
          <Check className="h-2.5 w-2.5" /> Done
        </span>
      ) : pending === 'rendering' ? (
        <span className="absolute inset-0 flex items-center justify-center">
          <span className="absolute inset-0 skeleton" />
          <RefreshCw className="relative h-5 w-5 animate-spin text-accent-2" />
        </span>
      ) : pending === 'queued' ? (
        <span className="absolute bottom-1.5 right-1.5 rounded bg-black/40 px-1 text-[9px] font-medium text-fg-3">Queued</span>
      ) : (
        <span className="absolute bottom-1.5 right-1.5 rounded bg-black/40 px-1 text-[9px] font-medium text-fg-3">Skipped</span>
      )}
    </>
  )

  return (
    <div
      className={cn(
        'group flex flex-col overflow-hidden rounded-xl border bg-surface-1 transition-[border-color,transform,box-shadow] duration-200 animate-fade-in',
        openable ? 'border-line hover:-translate-y-0.5 hover:border-line-3 hover:shadow-[0_16px_36px_-18px_rgb(0_0_0/0.9)]' : 'border-line',
        item?.error && (item.renderUrl ? 'border-warn/25' : 'border-bad/25'),
      )}
      style={{ animationDelay: `${Math.min(index, 30) * 12}ms` }}
    >
      <button
        type="button"
        disabled={!openable}
        onClick={() => openable && projectId && openProjectRoute(projectId)}
        data-tip={openable ? `Open “${name}” in the editor` : undefined}
        aria-label={openable ? `Open ${name}` : name}
        className="relative block aspect-square w-full checkerboard disabled:cursor-default"
      >
        {body}
      </button>
      <span className="flex items-center gap-1.5 px-2.5 py-1.5">
        <span className={cn('min-w-0 flex-1 truncate text-2xs font-medium', item?.renderUrl ? 'text-fg-2 group-hover:text-fg' : 'text-fg-4')}>{name}</span>
        {item?.renderUrl && item.error ? (
          <span data-tip={item.error} className="flex">
            <Badge tone="warn">
              <TriangleAlert />
            </Badge>
          </span>
        ) : (
          item?.renderUrl && (
            <span data-tip={item.seconds != null ? `Rendered in ${formatSeconds(item.seconds)}` : 'Rendered'} className="flex">
              <Badge tone="ok">
                <Check />
              </Badge>
            </span>
          )
        )}
        {item?.exportUrl && (
          <a
            href={item.exportUrl}
            download=""
            aria-label={`Download ${name} exports`}
            data-tip="Download this icon’s exports (.zip)"
            className="flex h-5 w-5 items-center justify-center rounded text-fg-4 opacity-0 transition-opacity hover:bg-white/10 hover:text-fg group-hover:opacity-100 focus-visible:opacity-100"
          >
            <FileArchive className="h-3 w-3" />
          </a>
        )}
        {item?.renderUrl && (
          <a
            href={item.renderUrl}
            download=""
            aria-label={`Download ${name} PNG`}
            data-tip="Download PNG"
            className="flex h-5 w-5 items-center justify-center rounded text-fg-4 opacity-0 transition-opacity hover:bg-white/10 hover:text-fg group-hover:opacity-100 focus-visible:opacity-100"
          >
            <Download className="h-3 w-3" />
          </a>
        )}
      </span>
    </div>
  )
}

