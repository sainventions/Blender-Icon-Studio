// Render orchestration: auto drafts / previews after edits, manual renders, renditions and the
// per-appearance "latest image" bookkeeping consumed by the stage.
import { create } from 'zustand'
import type { AppearanceId, Job, JobState, Quality, RenderRequest } from '../types'
import { errorMessage, jobsApi, projectsApi } from '../api'
import { APPEARANCE_IDS, renderSig } from '../lib/projectOps'
import { formatSeconds } from '../lib/format'
import { appearanceLabel, engineLabel } from '../lib/labels'
import { keepsSharperRender } from '../lib/renderPick'
import { onJobUpdate, useAppStore } from './app'
import { useEditor } from './editor'
import { toast } from './toasts'
import { useUi } from './ui'

export type RenderPurpose = 'live' | 'manual' | 'rendition'

export interface RenderEntry {
  jobId: string
  projectId: string
  appearance: AppearanceId
  quality: Quality
  purpose: RenderPurpose
  state: JobState
  progress: number
  message: string
  createdAt: number
  startedAt: number | null
  finishedAt: number | null
  url?: string
  width?: number
  height?: number
  seconds?: number
  engine?: string
  device?: string
  error?: string | null
  sig?: string
}

interface RenderState {
  projectId: string | null
  entries: Record<string, RenderEntry>
  /** Newest finished render per appearance (job id). */
  latest: Partial<Record<AppearanceId, string>>
  /** Newest finished rendition per appearance (job id). */
  renditions: Partial<Record<AppearanceId, string>>
  /** Entry pinned in the render view (from the history list); null = follow latest. */
  pinned: string | null
  liveError: string | null
  lastDone: string | null

  reset: (projectId: string | null) => void
  ingest: (job: Job, meta?: Partial<RenderEntry>) => void
  render: (quality: Quality, opts?: { size?: number; appearance?: AppearanceId; fullBleed?: boolean; purpose?: RenderPurpose }) => Promise<Job | null>
  renderRenditions: (quality?: Quality) => Promise<void>
  cancel: (jobId: string) => Promise<void>
  pin: (jobId: string | null) => void
}

/** Signature submitted per `${appearance}:${quality}` — skip identical auto renders. */
const submittedSig = new Map<string, string>()
const manualToasts = new Map<string, string>()

function isoMs(s: string | null | undefined): number | null {
  if (!s) return null
  const t = Date.parse(s)
  return Number.isFinite(t) ? t : null
}

function isQuality(v: unknown): v is Quality {
  return v === 'draft' || v === 'preview' || v === 'final' || v === 'ultra'
}
function isAppearance(v: unknown): v is AppearanceId {
  return typeof v === 'string' && (APPEARANCE_IDS as string[]).includes(v)
}

function resultUrl(job: Job): string | undefined {
  const r = job.result
  if (!r) return undefined
  if (typeof r.url === 'string') return r.url
  if (typeof r.path === 'string' && job.projectId) {
    const file = r.path.split(/[\\/]/).pop()
    return `/files/projects/${encodeURIComponent(job.projectId)}/renders/${file}`
  }
  return undefined
}

const STATE_RANK: Record<JobState, number> = { queued: 0, running: 1, done: 2, error: 2, cancelled: 2 }

/** Entries kept per open project. Every edit adds a draft (and usually a preview), and several selectors scan
 *  all entries on each update — so a long session must not grow the table without bound. */
const MAX_ENTRIES = 160

function pruneEntries(entries: Record<string, RenderEntry>, s: RenderState, incoming: string): Record<string, RenderEntry> {
  const ids = Object.keys(entries)
  if (ids.length <= MAX_ENTRIES) return entries
  const keep = new Set<string>([incoming])
  for (const id of [...Object.values(s.latest), ...Object.values(s.renditions), s.pinned, s.lastDone]) if (id) keep.add(id)
  const drop = ids
    .filter((id) => !keep.has(id) && entries[id].state !== 'queued' && entries[id].state !== 'running')
    .sort((a, b) => entries[a].createdAt - entries[b].createdAt)
    .slice(0, ids.length - MAX_ENTRIES)
  if (!drop.length) return entries
  const out = { ...entries }
  for (const id of drop) delete out[id]
  return out
}

const QUALITY_LABEL: Record<Quality, string> = { draft: 'Draft', preview: 'Preview', final: 'Final', ultra: 'Ultra' }

export const useRender = create<RenderState>((set, get) => ({
  projectId: null,
  entries: {},
  latest: {},
  renditions: {},
  pinned: null,
  liveError: null,
  lastDone: null,

  reset: (projectId) => {
    submittedSig.clear()
    set({ projectId, entries: {}, latest: {}, renditions: {}, pinned: null, liveError: null, lastDone: null })
  },

  ingest: (incoming, meta) => {
    const s = get()
    if (incoming.kind !== 'render' || !s.projectId || incoming.projectId !== s.projectId) return
    // Fast renders finish (WS) before the POST that queued them returns: never let a stale snapshot win.
    const known = useAppStore.getState().jobs[incoming.id]
    const job = known && STATE_RANK[known.state] > STATE_RANK[incoming.state] ? known : incoming
    const prev = s.entries[job.id]
    const req = job.request ?? {}
    const appearance =
      meta?.appearance ?? prev?.appearance ?? (isAppearance(req.appearance) ? req.appearance : undefined) ??
      useEditor.getState().project?.appearance ?? 'light'
    const quality = meta?.quality ?? prev?.quality ?? (isQuality(req.quality) ? req.quality : 'draft')
    const purpose = meta?.purpose ?? prev?.purpose ?? (req.rendition ? 'rendition' : req.live ? 'live' : 'manual')
    // Server-side auto previews (D7) render the state of the newest draft we submitted for this appearance.
    let sig = meta?.sig ?? prev?.sig
    if (!sig && req.auto && purpose === 'live') {
      sig = submittedSig.get(`${appearance}:draft`)
      if (sig) submittedSig.set(`${appearance}:${quality}`, sig)
    }
    const r = job.result ?? {}
    const entry: RenderEntry = {
      jobId: job.id,
      projectId: s.projectId,
      appearance,
      quality,
      purpose,
      state: job.state,
      progress: job.progress ?? 0,
      message: job.message ?? '',
      createdAt: isoMs(job.createdAt) ?? prev?.createdAt ?? Date.now(),
      startedAt: isoMs(job.startedAt) ?? prev?.startedAt ?? null,
      finishedAt: isoMs(job.finishedAt),
      url: resultUrl(job),
      width: typeof r.width === 'number' ? r.width : undefined,
      height: typeof r.height === 'number' ? r.height : undefined,
      seconds: typeof r.seconds === 'number' ? r.seconds : undefined,
      // 'Cycles' / 'EEVEE' (the server reports 'cycles' / 'eevee'): every chip, tooltip and toast shows this.
      engine: typeof r.engine === 'string' ? engineLabel(r.engine, quality) : undefined,
      device: typeof r.device === 'string' ? r.device : undefined,
      error: job.error,
      sig,
    }
    const patch: Partial<RenderState> = { entries: pruneEntries({ ...s.entries, [job.id]: entry }, s, job.id) }

    if (job.state === 'done' && entry.url) {
      const newer = (cur: string | undefined) => {
        const c = cur ? s.entries[cur] : undefined
        return !c || c.createdAt <= entry.createdAt
      }
      const shown = s.latest[appearance] ? s.entries[s.latest[appearance]!] : undefined
      if (newer(s.latest[appearance]) && !keepsSharperRender(shown, entry)) patch.latest = { ...s.latest, [appearance]: job.id }
      if (purpose === 'rendition' && newer(s.renditions[appearance]))
        patch.renditions = { ...s.renditions, [appearance]: job.id }
      const ld = s.lastDone ? s.entries[s.lastDone] : undefined
      if (!ld || (ld.finishedAt ?? 0) <= (entry.finishedAt ?? Date.now())) patch.lastDone = job.id
      if (purpose === 'live') patch.liveError = null
      if (purpose === 'live' && quality === 'draft' && prev?.state !== 'done') schedulePreviewFallback(entry)
    }
    if (job.state === 'error') {
      if (entry.sig) submittedSig.delete(`${appearance}:${quality}`)
      if (purpose === 'live') patch.liveError = job.error || 'Render failed'
    }
    if (job.state === 'cancelled' && entry.sig && submittedSig.get(`${appearance}:${quality}`) === entry.sig) {
      // Superseded live job: allow the same state to be re-requested later.
      submittedSig.delete(`${appearance}:${quality}`)
    }
    set(patch)

    // Manual render toasts
    const tid = manualToasts.get(job.id)
    if (tid) {
      const label = `${QUALITY_LABEL[quality]} render`
      if (job.state === 'queued' || job.state === 'running') {
        toast.update(tid, {
          title: `${label}${job.state === 'queued' ? ' · queued' : ''}`,
          description: job.message || undefined,
          progress: job.state === 'running' ? job.progress : null,
        })
      } else {
        manualToasts.delete(job.id)
        if (job.state === 'done') {
          const size = entry.width ? `${entry.width}×${entry.height ?? entry.width}` : ''
          toast.update(tid, {
            kind: 'success',
            title: `${label} finished`,
            description: [size, entry.engine, formatSeconds(entry.seconds)].filter(Boolean).join(' · '),
            progress: undefined,
            duration: 7000,
            action: { label: 'Show', onClick: () => { get().pin(job.id); useUi.getState().set({ stageMode: 'render' }) } },
            links: entry.url ? [{ label: 'Download PNG', href: entry.url, download: true }] : undefined,
          })
        } else if (job.state === 'error') {
          toast.update(tid, { kind: 'error', title: `${label} failed`, description: job.error ?? undefined, progress: undefined, duration: 9000, action: undefined })
        } else {
          toast.update(tid, { kind: 'info', title: `${label} cancelled`, progress: undefined, duration: 3000, action: undefined })
        }
      }
    }
  },

  render: async (quality, opts = {}) => {
    const ed = useEditor.getState()
    const p = ed.project
    if (!p) return null
    const purpose = opts.purpose ?? 'manual'
    const appearance = opts.appearance ?? p.appearance
    const ok = await ed.flushSave()
    if (!ok) return null
    const current = useEditor.getState().project
    if (!current || current.id !== p.id) return null
    const sig = renderSig(current)
    const req: RenderRequest = { quality, appearance, live: purpose === 'live' }
    if (opts.size) req.size = opts.size
    if (opts.fullBleed) req.fullBleed = true
    submittedSig.set(`${appearance}:${quality}`, sig)
    let tid: string | undefined
    if (purpose === 'manual') {
      tid = toast.progress(`${QUALITY_LABEL[quality]} render · queued`, {
        description: `${appearanceLabel(appearance, useAppStore.getState().presets.data)} · ${opts.size ?? useAppStore.getState().presets.data?.quality[quality]?.size ?? ''} px`,
      })
    }
    try {
      const job = await projectsApi.render(p.id, req)
      if (tid) {
        manualToasts.set(job.id, tid)
        toast.update(tid, { action: { label: 'Cancel', onClick: () => void get().cancel(job.id) } })
      }
      get().ingest(job, { appearance, quality, purpose, sig })
      useAppStore.getState().upsertJob(job)
      if (purpose === 'manual') get().pin(null)
      return job
    } catch (e) {
      submittedSig.delete(`${appearance}:${quality}`)
      if (purpose === 'live') set({ liveError: errorMessage(e) })
      if (tid) toast.update(tid, { kind: 'error', title: 'Render request failed', description: errorMessage(e), progress: undefined, duration: 8000 })
      return null
    }
  },

  renderRenditions: async (quality = 'draft') => {
    const ed = useEditor.getState()
    const p = ed.project
    if (!p) return
    const ok = await ed.flushSave()
    if (!ok) return
    const sig = renderSig(useEditor.getState().project ?? p)
    try {
      const jobs = await projectsApi.renditions(p.id, quality)
      jobs.forEach((job, i) => {
        const requested = job.request?.appearance
        const a = isAppearance(requested) ? requested : APPEARANCE_IDS[i]
        get().ingest(job, { appearance: a, quality, purpose: 'rendition', sig })
        useAppStore.getState().upsertJob(job)
      })
    } catch (e) {
      toast.error('Could not render renditions', { description: errorMessage(e) })
    }
  },

  cancel: async (jobId) => {
    try {
      await jobsApi.cancel(jobId)
    } catch (e) {
      toast.error('Cancel failed', { description: errorMessage(e) })
    }
  },

  pin: (jobId) => set({ pinned: jobId }),
}))

// Route every job update for the open project into the render store.
onJobUpdate((job) => useRender.getState().ingest(job))

// ------------------------------------------------------------------------------------------ selectors
export function latestFor(s: RenderState, a: AppearanceId): RenderEntry | null {
  const id = s.latest[a]
  return id ? (s.entries[id] ?? null) : null
}

/** Newest queued/running render entry (optionally only for one appearance). */
export function activeEntry(s: RenderState, a?: AppearanceId): RenderEntry | null {
  let best: RenderEntry | null = null
  for (const e of Object.values(s.entries)) {
    if (e.state !== 'queued' && e.state !== 'running') continue
    if (a && e.appearance !== a) continue
    if (!best || e.createdAt > best.createdAt) best = e
  }
  return best
}

// ------------------------------------------------------------------------------------------ automation
// Drafts: debounced ~350 ms after every render-relevant edit (live → coalesced server-side).
// Previews (D7): the server queues a Cycles preview ~1.2 s after a live draft finishes (request.auto). If no
// preview shows up for the newest draft in time (older server / disabled there) the client requests one itself,
// so there is always exactly one faithful preview per settled state.
const DRAFT_DELAY = 350
const PREVIEW_FALLBACK_MS = 2400
let previewFallbackTimer: number | undefined

function schedulePreviewFallback(draft: RenderEntry) {
  window.clearTimeout(previewFallbackTimer)
  previewFallbackTimer = window.setTimeout(() => {
    const p = useEditor.getState().project
    if (!p || !p.render.autoPreview || p.id !== draft.projectId || p.appearance !== draft.appearance) return
    const entries = Object.values(useRender.getState().entries)
    const newerDraft = entries.some((e) => e.purpose === 'live' && e.quality === 'draft' && e.createdAt > draft.createdAt)
    const preview = entries.some(
      (e) => e.quality !== 'draft' && e.purpose !== 'rendition' && e.createdAt >= draft.createdAt && e.state !== 'cancelled' && e.state !== 'error',
    )
    if (!newerDraft && !preview) void autoRender('preview')
  }, PREVIEW_FALLBACK_MS)
}

async function autoRender(quality: 'draft' | 'preview') {
  const p = useEditor.getState().project
  if (!p || useEditor.getState().busy) return
  const sys = useAppStore.getState().system
  if (sys && !sys.blender.found) return
  if (quality === 'preview' && !p.render.autoPreview) return
  const sig = renderSig(p)
  if (submittedSig.get(`${p.appearance}:${quality}`) === sig) return
  // A preview of exactly this state already exists → a draft adds nothing.
  if (quality === 'draft' && submittedSig.get(`${p.appearance}:preview`) === sig) return
  await useRender.getState().render(quality, { purpose: 'live', appearance: p.appearance })
}

/** Start debounced auto renders for the open project. Returns a disposer. */
export function startRenderAutomation(): () => void {
  let draftTimer: number | undefined
  let lastSeq = -1
  let lastAppearance: AppearanceId | null = null
  let lastProjectId: string | null = null

  const schedule = (fast: boolean) => {
    window.clearTimeout(draftTimer)
    window.clearTimeout(previewFallbackTimer)
    draftTimer = window.setTimeout(() => void autoRender('draft'), fast ? 60 : DRAFT_DELAY)
  }

  const check = () => {
    const s = useEditor.getState()
    const p = s.project
    if (!p) return
    if (p.id !== lastProjectId) {
      lastProjectId = p.id
      lastSeq = s.editSeq
      lastAppearance = p.appearance
      // Seed from jobs that already exist for this project (previous sessions / other tabs).
      void jobsApi
        .list(p.id)
        .then((jobs) => jobs.filter((j) => j.projectId === p.id && j.kind === 'render').forEach((j) => useRender.getState().ingest(j)))
        .catch(() => undefined)
        .finally(() => schedule(true))
      return
    }
    if (s.editSeq !== lastSeq) {
      lastSeq = s.editSeq
      lastAppearance = p.appearance
      schedule(false)
    } else if (p.appearance !== lastAppearance) {
      lastAppearance = p.appearance
      schedule(true)
    }
  }

  const unsub = useEditor.subscribe(check)
  check()
  return () => {
    unsub()
    window.clearTimeout(draftTimer)
    window.clearTimeout(previewFallbackTimer)
  }
}
