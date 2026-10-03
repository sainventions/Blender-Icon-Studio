import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDown,
  Clock,
  Copy,
  Cpu,
  FileUp,
  FolderOpen,
  Keyboard,
  Layers,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import type { ProjectSummary, SampleIcon } from '../../types'
import { errorMessage, projectsApi, samplesApi } from '../../api'
import { cn, relativeTime, shortGpuName, titleCase } from '../../lib/format'
import { openProjectRoute } from '../../lib/route'
import { useAppStore } from '../../store/app'
import { toast } from '../../store/toasts'
import { useUi } from '../../store/ui'
import { GlassMotif } from '../../components/GlassMotif'
import { Logo } from '../../components/icons'
import { Badge, Button, confirmDialog, EmptyState, IconButton, Kbd, Menu, Segmented, Skeleton, Spinner, StatusDot, useAnchor } from '../../components/ui'
import { isSvgFile, useFileDrop } from './useFileDrop'

export default function HomePage() {
  const loadSamples = useAppStore((s) => s.loadSamples)
  const loadProjects = useAppStore((s) => s.loadProjects)
  const openDialog = useUi((s) => s.openDialog)
  const fileInput = useRef<HTMLInputElement>(null)
  const samplesRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    void loadSamples()
    void loadProjects()
  }, [loadSamples, loadProjects])

  const dragging = useFileDrop((file) => openDialog('import', file))

  const pickFile = () => fileInput.current?.click()

  return (
    <div className="relative h-full overflow-y-auto overflow-x-hidden bg-app">
      <Backdrop />
      <TopNav onImport={pickFile} />

      <main className="relative mx-auto max-w-[1320px] px-8 pb-24">
        <Hero onImport={pickFile} onBrowse={() => samplesRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })} />
        <RecentProjects />
        <div ref={samplesRef} className="scroll-mt-16">
          <SampleGallery />
        </div>
      </main>

      <input
        ref={fileInput}
        type="file"
        accept=".svg,image/svg+xml"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          e.target.value = ''
          if (f) openDialog('import', f)
        }}
      />

      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-[700] flex items-center justify-center bg-app/70 backdrop-blur-md animate-fade-in">
          <div className="flex flex-col items-center gap-3 rounded-3xl border border-accent/50 bg-surface-1/80 px-16 py-12" style={{ animation: 'drop-pulse 1.6s ease-in-out infinite' }}>
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl accent-gradient shadow-[0_10px_40px_-10px_rgb(123_102_255/0.9)]">
              <FileUp className="h-7 w-7 text-white" />
            </div>
            <div className="text-lg font-semibold text-fg">Drop your SVG</div>
            <div className="text-xs text-fg-3">We'll split it into layers and build the 3D stack.</div>
          </div>
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------------------------------------ chrome
function Backdrop() {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-x-0 top-0 h-[900px] overflow-hidden">
      <div className="absolute -top-40 left-1/2 h-[700px] w-[1200px] -translate-x-1/2 rounded-full opacity-60 blur-[120px]" style={{ background: 'radial-gradient(closest-side, rgb(123 102 255 / 0.22), transparent)' }} />
      <div className="absolute right-[-200px] top-40 h-[500px] w-[700px] rounded-full opacity-50 blur-[120px]" style={{ background: 'radial-gradient(closest-side, rgb(47 212 240 / 0.14), transparent)' }} />
      <div
        className="absolute inset-0 opacity-[0.35]"
        style={{
          backgroundImage: 'linear-gradient(rgb(255 255 255 / 0.035) 1px, transparent 1px), linear-gradient(90deg, rgb(255 255 255 / 0.035) 1px, transparent 1px)',
          backgroundSize: '48px 48px',
          maskImage: 'radial-gradient(ellipse 70% 60% at 50% 20%, black, transparent 75%)',
          WebkitMaskImage: 'radial-gradient(ellipse 70% 60% at 50% 20%, black, transparent 75%)',
        }}
      />
    </div>
  )
}

function TopNav({ onImport }: { onImport: () => void }) {
  const openDialog = useUi((s) => s.openDialog)
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-app/60 backdrop-blur-xl">
      <div className="mx-auto flex h-12 max-w-[1320px] items-center gap-3 px-8">
        <Logo size={22} />
        <span className="text-[13px] font-semibold tracking-[-0.01em] text-fg">Blender Icon Studio</span>
        <Badge tone="accent" className="ml-1">Beta</Badge>
        <div className="flex-1" />
        <SystemPill />
        <IconButton label="Keyboard shortcuts" kbd="?" onClick={() => openDialog('shortcuts')}>
          <Keyboard />
        </IconButton>
        <Button variant="secondary" size="sm" icon={<Upload />} onClick={onImport}>
          Import SVG
        </Button>
      </div>
    </header>
  )
}

export function SystemPill({ className }: { className?: string }) {
  const system = useAppStore((s) => s.system)
  const systemError = useAppStore((s) => s.systemError)
  if (!system) {
    return (
      <div className={cn('flex h-7 items-center gap-2 rounded-full border border-line bg-white/[0.03] px-3 text-2xs text-fg-3', className)}>
        <StatusDot tone={systemError ? 'bad' : 'idle'} pulse={!systemError} />
        {systemError ? 'Server offline' : 'Connecting…'}
      </div>
    )
  }
  const w = system.worker.state
  const tone = !system.blender.found ? 'bad' : w === 'ready' ? 'ok' : w === 'busy' ? 'busy' : w === 'starting' ? 'warn' : w === 'error' ? 'bad' : 'idle'
  return (
    <div
      className={cn('flex h-7 items-center gap-2 rounded-full border border-line bg-white/[0.03] px-3 text-2xs text-fg-2', className)}
      data-tip={system.blender.found ? `Blender ${system.blender.version ?? ''} · worker ${w}` : 'Blender 5.0 not found'}
    >
      <StatusDot tone={tone} pulse={w === 'starting' || w === 'busy'} />
      <span className="font-medium">{system.blender.found ? `Blender ${system.blender.version?.split(' ')[0] ?? '5.0'}` : 'Blender missing'}</span>
      {system.gpu.name && (
        <>
          <span className="h-3 w-px bg-line-2" />
          <Cpu className="h-3 w-3 text-fg-3" />
          <span>
            <span className="font-semibold text-accent-2">{system.gpu.device ?? 'OPTIX'}</span> · {shortGpuName(system.gpu.name)}
          </span>
        </>
      )}
    </div>
  )
}

// ------------------------------------------------------------------------------------------ hero
function Hero({ onImport, onBrowse }: { onImport: () => void; onBrowse: () => void }) {
  const samples = useAppStore((s) => s.samples.data)
  const presets = useAppStore((s) => s.presets.data)
  return (
    <section className="relative grid min-h-[520px] grid-cols-1 items-center gap-6 pt-10 lg:grid-cols-[1.05fr_1fr]">
      <div className="relative z-10 animate-slide-up">
        <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-line-2 bg-white/[0.04] py-1 pl-1 pr-3 text-2xs text-fg-2 backdrop-blur">
          <span className="flex h-5 items-center gap-1 rounded-full accent-gradient px-2 text-3xs font-semibold text-white">
            <Sparkles className="h-3 w-3" /> New
          </span>
          Blender 5.0 · EEVEE drafts · Cycles + OptiX finals
        </div>
        <h1 className="font-display text-[52px] font-semibold leading-[1.02] tracking-[-0.035em] text-fg xl:text-[60px]">
          Flat SVG in.
          <br />
          <span className="text-gradient">Liquid Glass out.</span>
        </h1>
        <p className="mt-5 max-w-[540px] text-[15px] leading-relaxed text-fg-3">
          Split any icon into layers, extrude them into real glass, metal and candy, and render every Apple appearance on your GPU —
          then export for iOS, macOS, watchOS, Android, Windows and the web.
        </p>
        <div className="mt-8 flex flex-wrap items-center gap-3">
          <Button variant="primary" size="lg" icon={<Upload />} onClick={onImport} className="px-5">
            Import SVG
          </Button>
          <Button variant="secondary" size="lg" icon={<ArrowDown />} onClick={onBrowse}>
            Try a sample
          </Button>
          <span className="ml-1 hidden items-center gap-1.5 text-2xs text-fg-4 md:flex">
            or drop a file anywhere
          </span>
        </div>
        <dl className="mt-10 grid max-w-[540px] grid-cols-4 gap-4 border-t border-line pt-5">
          {[
            [samples ? samples.filter((x) => collectionOf(x) === 'corpus').length || samples.length : '—', 'sample icons'],
            [presets ? Object.keys(presets.materials).length : '—', 'materials'],
            [6, 'appearances'],
            [9, 'export targets'],
          ].map(([n, l]) => (
            <div key={String(l)}>
              <dt className="text-[20px] font-semibold tabular tracking-[-0.02em] text-fg">{n}</dt>
              <dd className="text-2xs text-fg-4">{l}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="relative hidden items-center justify-center lg:flex">
        <GlassMotif size={300} />
      </div>
    </section>
  )
}

// ------------------------------------------------------------------------------------------ recent projects
function RecentProjects() {
  const { data, loading, error } = useAppStore((s) => s.projects)
  const loadProjects = useAppStore((s) => s.loadProjects)
  const remove = useAppStore((s) => s.removeProjectSummary)
  const [busy, setBusy] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  if (!loading && !error && data && data.length === 0) return null
  const RECENT = 12
  const list = (showAll ? data : data?.slice(0, RECENT)) ?? []

  return (
    <section className="mt-6">
      <SectionHeader icon={<Clock />} title="Recent projects" count={data?.length}>
        {data && data.length > RECENT && (
          <Button size="xs" variant="ghost" onClick={() => setShowAll((v) => !v)}>
            {showAll ? 'Show recent' : `Show all ${data.length}`}
          </Button>
        )}
        <IconButton label="Refresh" onClick={() => void loadProjects()}>
          <RefreshCw className={cn(loading && 'animate-spin')} />
        </IconButton>
      </SectionHeader>
      {error && !data ? (
        <OfflineNote error={error} onRetry={() => void loadProjects()} />
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(188px,1fr))] gap-3">
          {!data &&
            Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="overflow-hidden rounded-xl border border-line bg-surface-1">
                <Skeleton className="aspect-[4/3] rounded-none" />
                <div className="space-y-1.5 p-3">
                  <Skeleton className="h-3 w-2/3" />
                  <Skeleton className="h-2.5 w-1/3" />
                </div>
              </div>
            ))}
          {list.map((p) => (
            <ProjectCard
              key={p.id}
              project={p}
              busy={busy === p.id}
              onDuplicate={async () => {
                setBusy(p.id)
                try {
                  const dup = await projectsApi.duplicate(p.id)
                  toast.success('Project duplicated', { description: dup.name })
                  void loadProjects()
                } catch (e) {
                  toast.error('Duplicate failed', { description: errorMessage(e) })
                } finally {
                  setBusy(null)
                }
              }}
              onDelete={async () => {
                const ok = await confirmDialog({
                  title: `Delete “${p.name}”?`,
                  description: 'The project, its renders and its exports are removed from disk. This cannot be undone.',
                  confirmLabel: 'Delete project',
                  danger: true,
                  icon: <Trash2 />,
                })
                if (!ok) return
                setBusy(p.id)
                try {
                  await projectsApi.remove(p.id)
                  remove(p.id)
                  toast.info('Project deleted', { description: p.name })
                } catch (e) {
                  toast.error('Delete failed', { description: errorMessage(e) })
                } finally {
                  setBusy(null)
                }
              }}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function ProjectCard({ project, busy, onDuplicate, onDelete }: { project: ProjectSummary; busy: boolean; onDuplicate: () => void; onDelete: () => void }) {
  const [imgOk, setImgOk] = useState(true)
  const thumb = project.thumbnail ? `${project.thumbnail}${project.thumbnail.includes('?') ? '&' : '?'}t=${encodeURIComponent(project.updatedAt)}` : null
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => openProjectRoute(project.id)}
      onKeyDown={(e) => e.key === 'Enter' && openProjectRoute(project.id)}
      className="group relative cursor-pointer overflow-hidden rounded-xl border border-line bg-surface-1 transition-[border-color,transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:border-line-3 hover:shadow-[0_18px_40px_-18px_rgb(0_0_0/0.9)]"
    >
      <div className="relative aspect-[4/3] overflow-hidden checkerboard">
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_50%_40%,rgb(143_125_255/0.12),transparent_70%)]" />
        {thumb && imgOk ? (
          <img
            src={thumb}
            alt=""
            loading="lazy"
            onError={() => setImgOk(false)}
            className="absolute inset-0 m-auto h-[82%] w-[82%] object-contain drop-shadow-[0_10px_20px_rgb(0_0_0/0.5)] transition-transform duration-300 group-hover:scale-[1.04]"
          />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-fg-4">
            <Layers className="h-8 w-8" />
          </div>
        )}
        {busy && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/40">
            <Spinner />
          </div>
        )}
        <div className="absolute right-2 top-2 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100" onClick={(e) => e.stopPropagation()}>
          <IconButton label="Duplicate" size="sm" className="bg-black/50 backdrop-blur" onClick={onDuplicate}>
            <Copy />
          </IconButton>
          <IconButton label="Delete" size="sm" className="bg-black/50 backdrop-blur hover:!text-bad" onClick={onDelete}>
            <Trash2 />
          </IconButton>
        </div>
      </div>
      <div className="flex items-center gap-2 px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold text-fg">{project.name}</div>
          <div className="mt-0.5 text-2xs text-fg-4">{relativeTime(project.updatedAt)}</div>
        </div>
        <Badge tip="Layers">
          <Layers /> {project.layerCount}
        </Badge>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------------------------------ samples
function SampleGallery() {
  const { data, loading, error } = useAppStore((s) => s.samples)
  const loadSamples = useAppStore((s) => s.loadSamples)
  const query = useUi((s) => s.sampleQuery)
  const setUi = useUi((s) => s.set)
  const openDialog = useUi((s) => s.openDialog)
  const searchRef = useRef<HTMLInputElement>(null)
  const [opening, setOpening] = useState<string | null>(null)
  const menu = useAnchor<HTMLElement>()
  const [menuSample, setMenuSample] = useState<SampleIcon | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === '/' && document.activeElement?.tagName !== 'INPUT') {
        e.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const [collection, setCollection] = useState<string>('corpus')
  const collections = useMemo(() => {
    const counts = new Map<string, number>()
    for (const s of data ?? []) counts.set(collectionOf(s), (counts.get(collectionOf(s)) ?? 0) + 1)
    // The user's own corpus first, then the other collections alphabetically.
    return new Map([...counts].sort(([a], [b]) => (a === 'corpus' ? -1 : b === 'corpus' ? 1 : a.localeCompare(b))))
  }, [data])
  const activeCollection = collections.has(collection) ? collection : 'all'

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!data) return []
    const inCollection = activeCollection === 'all' ? data : data.filter((s) => collectionOf(s) === activeCollection)
    return q ? inCollection.filter((s) => s.name.toLowerCase().includes(q) || s.file.toLowerCase().includes(q)) : inCollection
  }, [data, query, activeCollection])

  const open = async (s: SampleIcon) => {
    if (opening) return
    setOpening(s.name)
    try {
      const p = await projectsApi.createFromSample(s.name, 'smart')
      openProjectRoute(p.id)
    } catch (e) {
      toast.error(`Could not open ${s.name}`, { description: errorMessage(e) })
    } finally {
      setOpening(null)
    }
  }

  const customize = async (s: SampleIcon) => {
    try {
      const res = await fetch(s.url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      openDialog('import', new File([blob], s.file.split(/[\\/]/).pop() || `${s.name}.svg`, { type: 'image/svg+xml' }))
    } catch (e) {
      toast.error('Could not load sample', { description: errorMessage(e) })
    }
  }

  return (
    <section className="mt-12">
      <SectionHeader icon={<FolderOpen />} title="Sample icons" count={filtered.length}>
        {collections.size > 1 && (
          <Segmented
            size="sm"
            value={activeCollection}
            onChange={setCollection}
            options={[
              ...[...collections.entries()].map(([c, n]) => ({ value: c, label: `${COLLECTION_LABEL[c] ?? titleCase(c)} · ${n}` })),
              { value: 'all', label: `All · ${data?.length ?? 0}` },
            ]}
          />
        )}
        <label className="relative flex h-8 w-[260px] items-center rounded-lg border border-line bg-surface-1/80 pl-8 pr-2 transition-colors focus-within:border-accent/60">
          <Search className="absolute left-2.5 h-3.5 w-3.5 text-fg-4" />
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setUi({ sampleQuery: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setUi({ sampleQuery: '' })
              if (e.key === 'Enter' && filtered[0]) void open(filtered[0])
            }}
            placeholder="Search samples"
            className="h-full w-full bg-transparent text-xs text-fg outline-none placeholder:text-fg-4"
          />
          {query ? (
            <button type="button" aria-label="Clear search" onClick={() => setUi({ sampleQuery: '' })} className="text-fg-4 hover:text-fg">
              <X className="h-3.5 w-3.5" />
            </button>
          ) : (
            <Kbd>/</Kbd>
          )}
        </label>
      </SectionHeader>

      {error && !data ? (
        <OfflineNote error={error} onRetry={() => void loadSamples(true)} />
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(118px,1fr))] gap-2.5">
          {!data &&
            loading !== false &&
            Array.from({ length: 24 }).map((_, i) => (
              <div key={i} className="rounded-xl border border-line bg-surface-1 p-3">
                <Skeleton className="mx-auto aspect-square w-[78%] rounded-[22%]" />
                <Skeleton className="mx-auto mt-3 h-2.5 w-1/2" />
              </div>
            ))}
          {filtered.map((s, i) => (
            <SampleCard
              key={s.name}
              sample={s}
              index={i}
              opening={opening === s.name}
              onOpen={() => void open(s)}
              onMenu={(el) => {
                setMenuSample(s)
                menu.openAt(el)
              }}
            />
          ))}
        </div>
      )}
      {data && filtered.length === 0 && (
        <EmptyState icon={<Search />} title={`No samples match “${query}”`}>
          Try another name, or import your own SVG.
        </EmptyState>
      )}
      <Menu
        open={menu.open}
        onClose={menu.close}
        anchor={menu.anchor}
        placement="bottom-end"
        items={
          menuSample
            ? [
                { label: 'Open (smart split)', icon: <Sparkles />, onSelect: () => void open(menuSample) },
                { label: 'Import with options…', icon: <SlidersHorizontal />, description: 'Choose the split strategy and preview layers first.', onSelect: () => void customize(menuSample) },
              ]
            : []
        }
      />
    </section>
  )
}

function SampleCard({
  sample,
  index,
  opening,
  onOpen,
  onMenu,
}: {
  sample: SampleIcon
  index: number
  opening: boolean
  onOpen: () => void
  onMenu: (el: HTMLElement) => void
}) {
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  return (
    <button
      type="button"
      onClick={(e) => (e.shiftKey ? onMenu(e.currentTarget) : onOpen())}
      onContextMenu={(e) => {
        e.preventDefault()
        onMenu(e.currentTarget)
      }}
      data-tip={`Open ${sample.name}`}
      data-tip-kbd="Shift+Click"
      className="group relative flex cursor-pointer flex-col items-center rounded-xl border border-line bg-surface-1/70 px-2 pb-2.5 pt-3 transition-[border-color,background-color,transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:border-line-3 hover:bg-surface-2 hover:shadow-[0_16px_36px_-18px_rgb(0_0_0/0.9)] animate-fade-in"
      style={{ animationDelay: `${Math.min(index, 40) * 12}ms` }}
    >
      <div className="relative aspect-square w-[78%]">
        {!loaded && !failed && <Skeleton className="absolute inset-0 rounded-[22%]" />}
        {failed ? (
          <div className="absolute inset-0 flex items-center justify-center rounded-[22%] bg-surface-3 text-fg-4">
            <Layers className="h-6 w-6" />
          </div>
        ) : (
          <img
            src={samplesApi.thumbnailUrl(sample.name)}
            alt={sample.name}
            loading="lazy"
            decoding="async"
            onLoad={() => setLoaded(true)}
            onError={() => setFailed(true)}
            className={cn(
              'absolute inset-0 h-full w-full object-contain drop-shadow-[0_8px_14px_rgb(0_0_0/0.45)] transition-[opacity,transform] duration-300 group-hover:scale-[1.06]',
              loaded ? 'opacity-100' : 'opacity-0',
            )}
          />
        )}
        {opening && (
          <div className="absolute inset-0 flex items-center justify-center rounded-[22%] bg-black/50 backdrop-blur-sm">
            <Spinner className="text-white" />
          </div>
        )}
      </div>
      <div className="mt-2.5 w-full truncate text-center text-2xs font-medium text-fg-3 group-hover:text-fg">{sample.name}</div>
    </button>
  )
}

// ------------------------------------------------------------------------------------------ bits
const COLLECTION_LABEL: Record<string, string> = { corpus: 'App icons', svgtests: 'Test SVGs' }

/** The server tags samples with their source folder (additive field, not in the shared contract). */
function collectionOf(s: SampleIcon): string {
  return (s as SampleIcon & { collection?: string }).collection ?? 'corpus'
}

function SectionHeader({ icon, title, count, children }: { icon: React.ReactNode; title: string; count?: number; children?: React.ReactNode }) {
  return (
    <div className="mb-4 flex items-center gap-2.5">
      <span className="flex h-6 w-6 items-center justify-center rounded-md border border-line-2 bg-white/[0.03] text-fg-3 [&>svg]:h-3.5 [&>svg]:w-3.5">{icon}</span>
      <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-fg">{title}</h2>
      {count != null && <span className="text-2xs tabular text-fg-4">{count}</span>}
      <div className="flex-1" />
      {children}
    </div>
  )
}

function OfflineNote({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-bad/20 bg-bad/[0.06] px-4 py-3 text-xs text-fg-2">
      <StatusDot tone="bad" />
      <span className="flex-1">{error}</span>
      <Button size="xs" variant="secondary" icon={<RefreshCw />} onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}

export { isSvgFile }
