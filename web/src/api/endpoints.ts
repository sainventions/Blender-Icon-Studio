// Typed wrappers for every REST endpoint in docs/PLAN.md §8.
import type {
  AnimateRequest,
  ExportRequest,
  GeometryBundle,
  Job,
  Presets,
  Project,
  ProjectSummary,
  Quality,
  RenderRequest,
  SampleIcon,
  SplitStrategy,
  SystemStatus,
} from '../types'
import { http, apiUrl } from './client'

const enc = encodeURIComponent

export const systemApi = {
  status: (signal?: AbortSignal) => http.get<SystemStatus>('/system', { signal }),
  restartWorker: () => http.post<unknown>('/system/worker/restart'),
}

export const presetsApi = {
  get: () => http.get<Presets>('/presets'),
}

export const samplesApi = {
  list: () => http.get<SampleIcon[]>('/samples'),
  thumbnailUrl: (name: string) => apiUrl(`/samples/${enc(name)}/thumbnail.png`),
}

export const projectsApi = {
  list: () => http.get<ProjectSummary[]>('/projects'),
  createFromSample: (sample: string, strategy?: SplitStrategy) =>
    http.post<Project>('/projects', strategy ? { sample, strategy } : { sample }),
  upload: (file: File | Blob, filename: string, strategy?: SplitStrategy) => {
    const form = new FormData()
    form.append('file', file, filename)
    if (strategy) form.append('strategy', strategy)
    return http.post<Project>('/projects', undefined, { form })
  },
  get: (id: string, signal?: AbortSignal) => http.get<Project>(`/projects/${enc(id)}`, { signal }),
  save: (project: Project, opts?: { keepalive?: boolean }) =>
    http.put<Project>(`/projects/${enc(project.id)}`, project, { keepalive: opts?.keepalive }),
  remove: (id: string) => http.del<unknown>(`/projects/${enc(id)}`),
  duplicate: (id: string) => http.post<Project>(`/projects/${enc(id)}/duplicate`),
  sourceSvgUrl: (id: string) => apiUrl(`/projects/${enc(id)}/source.svg`),

  split: (id: string, strategy: SplitStrategy) => http.post<Project>(`/projects/${enc(id)}/split`, { strategy }),
  mergeLayers: (id: string, layerIds: string[]) =>
    http.post<Project>(`/projects/${enc(id)}/layers/merge`, { layerIds }),
  splitLayer: (id: string, layerId: string, mode: 'elements' | 'islands') =>
    http.post<Project>(`/projects/${enc(id)}/layers/${enc(layerId)}/split`, { mode }),
  moveElements: (id: string, elementIds: string[], toLayerId: string | null) =>
    http.post<Project>(`/projects/${enc(id)}/elements/move`, { elementIds, toLayerId }),

  geometry: (id: string, signal?: AbortSignal) => http.get<GeometryBundle>(`/projects/${enc(id)}/geometry`, { signal }),
  layerThumbnailUrl: (id: string, layerId: string, version?: string) =>
    apiUrl(`/projects/${enc(id)}/layers/${enc(layerId)}/thumbnail.png${version ? `?v=${enc(version)}` : ''}`),

  render: (id: string, req: RenderRequest) => http.post<Job>(`/projects/${enc(id)}/render`, req),
  renditions: (id: string, quality: Quality) => http.post<Job[]>(`/projects/${enc(id)}/renditions`, { quality }),
  animate: (id: string, req: AnimateRequest) => http.post<Job>(`/projects/${enc(id)}/animate`, req),
  export: (id: string, req: ExportRequest) => http.post<Job>(`/projects/${enc(id)}/export`, req),
  blend: (id: string, open: boolean) => http.post<Job>(`/projects/${enc(id)}/blend`, { open }),
}

export const jobsApi = {
  /** All jobs, or only one project's (server-side filter). */
  list: (projectId?: string) => http.get<Job[]>(projectId ? `/jobs?projectId=${enc(projectId)}` : '/jobs'),
  get: (id: string) => http.get<Job>(`/jobs/${enc(id)}`),
  cancel: (id: string) => http.del<unknown>(`/jobs/${enc(id)}`),
}
