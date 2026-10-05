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
      doneDescription: (j) => (j.result?.opened === false ? 'Saved the .blend, but Blender could not be launched.' : 'Blender 5.0 is starting with your scene.'),
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

let isoRaf = 0
/** Where the running swing (animateIso) is heading. */
let isoGoal = 0

/** The CAD-style view angle shown right now: the animation's value while one runs, else project.camera.iso. */
export function currentIso(): number {
  const anim = useUi.getState().isoAnim
  return anim ?? useEditor.getState().project?.camera.iso ?? 0
}

/** Set project.camera.iso (0 = head-on … 1 = isometric, real layer distances; PLAN §11 View). The CAD view is
 *  orthographic, so a perspective camera switches back to the front projection. One undo step per gesture (slider
 *  drag); `step` = a discrete change (X / I key, Front / Iso buttons) that is always its own undo step. */
export function setIso(iso: number, step = false) {
  const v = Math.round(Math.min(1, Math.max(0, iso)) * 1000) / 1000
  cancelAnimationFrame(isoRaf)
  if (useUi.getState().isoAnim !== null) useUi.setState({ isoAnim: null })
  useEditor.getState().commit(
    (p) => (p.camera.iso === v && p.camera.view === 'front' ? p : { ...p, camera: { ...p.camera, iso: v, view: 'front' } }),
    step ? {} : { coalesce: 'camera.iso' },
  )
}

/** Swing the view to `target` (default: toggle top-down / head-on ↔ isometric, the X key), animated in the live
 *  view, committed ONCE at the end as its own undo step (never merged into a following slider drag or toggle). */
export function animateIso(target?: number) {
  const from = currentIso()
  // a toggle while a swing runs reverses that swing (toward the other end than the one it was heading to)
  const heading = useUi.getState().isoAnim !== null ? isoGoal : from
  const to = target ?? (heading > 0.5 ? 0 : 1)
  isoGoal = to
  cancelAnimationFrame(isoRaf)
  if (Math.abs(to - from) < 1e-4) return setIso(to, true)
  const t0 = performance.now()
  const dur = 560
  const step = (t: number) => {
    const k = Math.min(1, (t - t0) / dur)
    const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2
    if (k < 1) {
      useUi.setState({ isoAnim: from + (to - from) * e })
      isoRaf = requestAnimationFrame(step)
    } else setIso(to, true)
  }
  useUi.setState({ isoAnim: from }) // running from now on (a second press before the first frame reverses it)
  isoRaf = requestAnimationFrame(step)
}
