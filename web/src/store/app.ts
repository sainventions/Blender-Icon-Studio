// App-wide state: presets, system status, websocket, samples, recent projects and the job table.
import { create } from 'zustand'
import type { Job, Presets, ProjectSummary, SampleIcon, SystemStatus, WsEvent } from '../types'
import { connectEvents, errorMessage, jobsApi, presetsApi, projectsApi, samplesApi, systemApi, type WsState } from '../api'

type Load<T> = { data: T | null; loading: boolean; error: string | null }
const idle = <T,>(): Load<T> => ({ data: null, loading: false, error: null })

export type JobListener = (job: Job) => void

interface AppState {
  presets: Load<Presets>
  system: SystemStatus | null
  systemError: string | null
  ws: WsState
  samples: Load<SampleIcon[]>
  projects: Load<ProjectSummary[]>
  jobs: Record<string, Job>

  loadPresets: (force?: boolean) => Promise<Presets | null>
  loadSamples: (force?: boolean) => Promise<void>
  loadProjects: () => Promise<void>
  refreshSystem: () => Promise<void>
  upsertJob: (job: Job) => void
  removeProjectSummary: (id: string) => void
}

const jobListeners = new Set<JobListener>()
/** Subscribe to every job update (REST responses and WS events). */
export function onJobUpdate(fn: JobListener): () => void {
  jobListeners.add(fn)
  return () => jobListeners.delete(fn)
}

const JOB_STATE_RANK: Record<Job['state'], number> = { queued: 0, running: 1, done: 2, error: 2, cancelled: 2 }

export const useAppStore = create<AppState>((set, get) => ({
  presets: idle(),
  system: null,
  systemError: null,
  ws: 'connecting',
  samples: idle(),
  projects: idle(),
  jobs: {},

  loadPresets: async (force) => {
    const cur = get().presets
    if (cur.data && !force) return cur.data
    set({ presets: { ...cur, loading: true, error: null } })
    try {
      const data = await presetsApi.get()
      set({ presets: { data, loading: false, error: null } })
      return data
    } catch (e) {
      set({ presets: { data: cur.data, loading: false, error: errorMessage(e) } })
      return cur.data
    }
  },

  loadSamples: async (force) => {
    const cur = get().samples
    if ((cur.data && !force) || cur.loading) return
    set({ samples: { ...cur, loading: true, error: null } })
    try {
      const data = await samplesApi.list()
      data.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true }))
      set({ samples: { data, loading: false, error: null } })
    } catch (e) {
      set({ samples: { data: cur.data, loading: false, error: errorMessage(e) } })
    }
  },

  loadProjects: async () => {
    const cur = get().projects
    set({ projects: { ...cur, loading: true, error: null } })
    try {
      const data = await projectsApi.list()
      set({ projects: { data, loading: false, error: null } })
    } catch (e) {
      set({ projects: { data: cur.data, loading: false, error: errorMessage(e) } })
    }
  },

  refreshSystem: async () => {
    try {
      const status = await systemApi.status()
      set({ system: status, systemError: null })
    } catch (e) {
      set({ systemError: errorMessage(e) })
    }
  },

  upsertJob: (job) => {
    const prev = get().jobs[job.id]
    // Ignore stale updates that would move a job backwards (e.g. a late REST response after a WS 'done').
    if (prev && JOB_STATE_RANK[prev.state] > JOB_STATE_RANK[job.state]) return
    const jobs = { ...get().jobs, [job.id]: job }
    const ids = Object.keys(jobs)
    if (ids.length > 300) {
      ids
        .sort((a, b) => Date.parse(jobs[a].createdAt) - Date.parse(jobs[b].createdAt))
        .slice(0, ids.length - 300)
        .forEach((id) => {
          if (jobs[id].state !== 'running' && jobs[id].state !== 'queued') delete jobs[id]
        })
    }
    set({ jobs })
    jobListeners.forEach((fn) => fn(job))
  },

  removeProjectSummary: (id) => {
    const cur = get().projects
    if (cur.data) set({ projects: { ...cur, data: cur.data.filter((p) => p.id !== id) } })
  },
}))

// ------------------------------------------------------------------------------------------ bootstrapping
let started = false
let pollTimer: number | undefined

/** Connect the websocket and start background refreshes. Idempotent. */
export function startAppServices() {
  if (started) return
  started = true
  const s = useAppStore.getState()
  void s.loadPresets()
  void s.refreshSystem()

  const handle = (e: WsEvent) => {
    switch (e.type) {
      case 'job':
        useAppStore.getState().upsertJob(e.job)
        break
      case 'system':
        useAppStore.setState({ system: e.status, systemError: null })
        break
      case 'project':
        // Saved/deleted elsewhere: refresh the home list lazily.
        if (e.event === 'deleted') useAppStore.getState().removeProjectSummary(e.projectId)
        break
    }
  }

  connectEvents({
    onEvent: handle,
    onState: (ws) => useAppStore.setState({ ws }),
    onOpen: async (reconnect) => {
      const st = useAppStore.getState()
      void st.refreshSystem()
      if (!st.presets.data) void st.loadPresets()
      if (reconnect) {
        // Resync jobs we may have missed while disconnected.
        try {
          const jobs = await jobsApi.list()
          jobs.forEach((j) => useAppStore.getState().upsertJob(j))
        } catch {
          /* ignore */
        }
      }
    },
  })

  // Fallback polling while the socket is down (the server pushes system status every ~2 s otherwise).
  pollTimer = window.setInterval(() => {
    if (useAppStore.getState().ws !== 'open') void useAppStore.getState().refreshSystem()
  }, 5000)
}

export function stopAppServices() {
  window.clearInterval(pollTimer)
}
