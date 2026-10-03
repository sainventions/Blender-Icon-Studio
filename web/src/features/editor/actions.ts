// Editor-level actions shared by the top bar, menus and keyboard shortcuts.
import type { Job, Platform } from '../../types'
import { errorMessage, jobsApi, projectsApi } from '../../api'
import { openProjectRoute } from '../../lib/route'
import { formatSeconds } from '../../lib/format'
import { onJobUpdate, useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { toast, type ToastLink } from '../../store/toasts'
import { useUi } from '../../store/ui'

/** Mirror a job's progress in a toast until it finishes. */
export function trackJobToast(
  job: Job,
  labels: { title: string; done: string; doneDescription?: (j: Job) => string | undefined; links?: (j: Job) => ToastLink[] | undefined },
) {
  const id = toast.progress(labels.title, {
    description: job.message || 'Queued',
    action: { label: 'Cancel', onClick: () => void jobsApi.cancel(job.id).catch(() => undefined) },
  })
  const apply = (j: Job) => {
    if (j.id !== job.id) return
    if (j.state === 'queued' || j.state === 'running') {
      toast.update(id, { description: j.message || (j.state === 'queued' ? 'Queued' : 'Running…'), progress: j.state === 'running' ? j.progress : null })
      return
    }
    off()
    if (j.state === 'done') {
      toast.update(id, {
        kind: 'success',
        title: labels.done,
        description: labels.doneDescription?.(j) ?? (typeof j.result?.seconds === 'number' ? formatSeconds(j.result.seconds) : undefined),
        links: labels.links?.(j),
        progress: undefined,
        action: undefined,
        duration: 9000,
      })
    } else if (j.state === 'error') {
      toast.update(id, { kind: 'error', title: `${labels.title} failed`, description: j.error ?? undefined, progress: undefined, action: undefined, duration: 10000 })
    } else {
      toast.update(id, { kind: 'info', title: `${labels.title} cancelled`, progress: undefined, action: undefined, duration: 3000 })
    }
  }
  const off = onJobUpdate(apply)
  // The job may already have finished (WS) before the request that created it returned.
  apply(useAppStore.getState().jobs[job.id] ?? job)
}

export async function openInBlender() {
  const ed = useEditor.getState()
  const p = ed.project
  if (!p) return
  if (!(await ed.flushSave())) return
  try {
    const job = await projectsApi.blend(p.id, true)
    useAppStore.getState().upsertJob(job)
    trackJobToast(job, {
      title: 'Building .blend scene',
      done: 'Opened in Blender',
      doneDescription: (j) => (j.result?.opened === false ? 'Saved the .blend — Blender could not be launched.' : 'Blender 5.0 is starting with your scene.'),
      links: (j) => (typeof j.result?.url === 'string' ? [{ label: 'Download .blend', href: j.result.url, download: true }] : undefined),
    })
  } catch (e) {
    toast.error('Open in Blender failed', { description: errorMessage(e) })
  }
}

export async function duplicateProject() {
  const ed = useEditor.getState()
  const p = ed.project
  if (!p) return
  await ed.flushSave()
  try {
    const dup = await projectsApi.duplicate(p.id)
    toast.success('Project duplicated', { description: dup.name, action: { label: 'Back to original', onClick: () => openProjectRoute(p.id) } })
    openProjectRoute(dup.id)
  } catch (e) {
    toast.error('Duplicate failed', { description: errorMessage(e) })
  }
}

export const PLATFORM_IDS: Platform[] = ['ios', 'macos', 'watchos', 'android', 'windows', 'web', 'free']

/** Switch the target platform; the plate takes that platform's shape (presets.json "platforms"). */
export function setPlatform(platform: Platform) {
  const spec = useAppStore.getState().presets.data?.platforms[platform]
  useEditor.getState().commit(
    (p) =>
      p.canvas.platform === platform
        ? p
        : { ...p, canvas: { ...p.canvas, platform, shape: spec?.shape ?? p.canvas.shape } },
    { coalesce: 'canvas.platform' },
  )
}

let explodeRaf = 0
/** Animate the UI explode amount toward a target (X toggles 0 ↔ 1). */
export function animateExplode(target?: number) {
  const from = useUi.getState().explode
  const to = target ?? (from > 0.5 ? 0 : 1)
  cancelAnimationFrame(explodeRaf)
  const t0 = performance.now()
  const dur = 520
  const step = (t: number) => {
    const k = Math.min(1, (t - t0) / dur)
    const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2
    useUi.setState({ explode: from + (to - from) * e })
    if (k < 1) explodeRaf = requestAnimationFrame(step)
  }
  explodeRaf = requestAnimationFrame(step)
}
