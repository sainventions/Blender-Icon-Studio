import { useEffect, useState } from 'react'
import type { Job } from '../../types'
import { jobsApi } from '../../api'
import { useAppStore } from '../../store/app'
import { trackJobToast } from '../editor/actions'

/** Live view of a job (WS-driven, with a slow REST poll as a safety net while it runs). */
export function useJob(jobId: string | null): Job | null {
  const job = useAppStore((s) => (jobId ? (s.jobs[jobId] ?? null) : null))
  const [, force] = useState(0)
  useEffect(() => {
    if (!jobId) return
    const t = window.setInterval(async () => {
      const cur = useAppStore.getState().jobs[jobId]
      if (cur && cur.state !== 'queued' && cur.state !== 'running') return
      if (useAppStore.getState().ws === 'open' && cur) return
      try {
        const j = await jobsApi.get(jobId)
        useAppStore.getState().upsertJob(j)
        force((x) => x + 1)
      } catch {
        /* ignore */
      }
    }, 2500)
    return () => window.clearInterval(t)
  }, [jobId])
  return job
}

export function isActive(job: Job | null): boolean {
  return !!job && (job.state === 'queued' || job.state === 'running')
}

const backgrounded = new Set<string>()

/** A dialog closed while its job still runs: keep reporting that job in a toast (progress, cancel, download).
 *  `finished: true` also reports a job that already ended (the dialog closed before it could show the result). */
export function continueInToast(job: Job | null, labels: Parameters<typeof trackJobToast>[1], opts: { finished?: boolean } = {}) {
  if (!job || backgrounded.has(job.id) || (!opts.finished && !isActive(job))) return
  backgrounded.add(job.id)
  trackJobToast(job, labels)
}
