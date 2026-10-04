import { useState } from 'react'
import { Clapperboard, Download, LoaderCircle, Orbit, Rotate3d, Sparkles, SunMedium, Waves, Boxes, X, CircleAlert } from 'lucide-react'
import type { AnimateRequest, Quality } from '../../types'
import { errorMessage, jobsApi, projectsApi } from '../../api'
import { cn, formatSeconds } from '../../lib/format'
import { ANIMATION_KINDS } from '../../lib/meta'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { toast } from '../../store/toasts'
import { useUi } from '../../store/ui'
import { Button, Dialog, ProgressBar, Row, Segmented, SliderRow } from '../../components/ui'
import { continueInToast, isActive, useJob } from './useJob'

const KIND_ICON: Record<AnimateRequest['kind'], React.ReactNode> = {
  tilt: <Rotate3d />,
  turntable: <Orbit />,
  float: <Waves />,
  'light-sweep': <SunMedium />,
  iso: <Boxes />,
  explode: <Boxes />,
}

const ANIMATE_TOAST: Parameters<typeof continueInToast>[1] = {
  title: 'Rendering animation',
  done: 'Animation ready',
  links: (j) => {
    const href = j.result?.zip ?? j.result?.video ?? j.result?.url
    return typeof href === 'string' ? [{ label: 'Download', href, download: true }] : undefined
  },
}

export function AnimateDialog() {
  const open = useUi((s) => s.dialog === 'animate')
  const project = useEditor((s) => s.project)
  const presets = useAppStore((s) => s.presets.data)
  const [req, setReq] = useState<AnimateRequest>({ kind: 'tilt', frames: 72, fps: 30, quality: 'draft', size: 512, format: 'mp4' })
  const [jobId, setJobId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const job = useJob(jobId)
  const close = () => {
    continueInToast(job, ANIMATE_TOAST)
    useUi.getState().openDialog(null)
  }

  if (!project) return null
  const running = isActive(job)
  const done = job?.state === 'done'
  const result = job?.result ?? {}
  const zip = typeof result.zip === 'string' ? result.zip : null
  const frames = Array.isArray(result.frames) ? (result.frames as unknown[]).filter((f): f is string => typeof f === 'string') : []
  const resultUrl = (typeof result.video === 'string' && result.video) || (typeof result.url === 'string' && result.url) || null
  // A PNG sequence comes back as a .zip (result.url === result.zip): preview its first frame instead.
  const url = resultUrl && resultUrl !== zip && !/\.zip(\?|$)/i.test(resultUrl) ? resultUrl : (frames[0] ?? null)
  const download = zip ?? resultUrl
  const isVideo = !!url && /\.(mp4|webm|mov)(\?|$)/i.test(url)
  const heavy = (req.quality === 'final' || req.quality === 'ultra') && req.size > 512
  const perFrame = req.quality === 'draft' ? 0.3 : req.quality === 'preview' ? 1.2 : req.quality === 'final' ? 8 : 30

  const start = async () => {
    setSubmitting(true)
    try {
      await useEditor.getState().flushSave()
      const j = await projectsApi.animate(project.id, req)
      useAppStore.getState().upsertJob(j)
      setJobId(j.id)
      // Closed while the request was in flight: report the job in a toast instead.
      if (useUi.getState().dialog !== 'animate') continueInToast(useAppStore.getState().jobs[j.id] ?? j, ANIMATE_TOAST, { finished: true })
    } catch (e) {
      toast.error('Animation failed to start', { description: errorMessage(e) })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={close}
      width={680}
      icon={<Clapperboard />}
      title="Animate"
      description="Render a short motion clip of your icon — perfect for App Store previews and launch posts."
      footer={
        job ? (
          <>
            {running && (
              <Button variant="ghost" icon={<X />} onClick={() => void jobsApi.cancel(job.id)}>
                Cancel
              </Button>
            )}
            <div className="flex-1" />
            {!running && (
              <Button variant="secondary" onClick={() => setJobId(null)}>
                {done ? 'New animation' : 'Back'}
              </Button>
            )}
            {done && download && (
              <a href={download} download="" className="inline-flex h-8 items-center gap-2 rounded-md px-3 text-xs font-medium text-white accent-gradient">
                <Download className="h-4 w-4" /> Download {zip ? 'frames (.zip)' : req.format.toUpperCase()}
              </a>
            )}
          </>
        ) : (
          <>
            <span className="text-2xs text-fg-4">
              {formatSeconds(req.frames / req.fps)} clip · {req.frames} frames · est. render ~{formatSeconds(req.frames * perFrame)}
            </span>
            <div className="flex-1" />
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" icon={<Sparkles />} loading={submitting} onClick={() => void start()} data-autofocus>
              Render animation
            </Button>
          </>
        )
      }
    >
      {job ? (
        job.state === 'done' ? (
          <div className="overflow-hidden rounded-xl border border-line checkerboard">
            {url ? (
              isVideo ? (
                <video src={url} autoPlay loop muted playsInline controls className="mx-auto block h-[min(52vh,440px)] w-full object-contain" />
              ) : (
                <img src={url} alt="Animation" className="mx-auto block h-[min(52vh,440px)] w-full object-contain" />
              )
            ) : (
              <div className="py-10 text-center text-2xs text-fg-3">Finished — download the frames below.</div>
            )}
            <div className="border-t border-line bg-surface-1/80 px-3 py-2 text-3xs text-fg-4">
              {frames.length ? `${frames.length} frames · ` : ''}
              {typeof result.seconds === 'number' ? `rendered in ${formatSeconds(result.seconds)}` : ''}
            </div>
          </div>
        ) : job.state === 'error' || job.state === 'cancelled' ? (
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <CircleAlert className={cn('h-8 w-8', job.state === 'error' ? 'text-bad' : 'text-fg-3')} />
            <div className="text-sm font-semibold text-fg">{job.state === 'error' ? 'Animation failed' : 'Cancelled'}</div>
            {job.error && <div className="max-w-[520px] break-words text-2xs text-fg-3">{job.error}</div>}
          </div>
        ) : (
          <div className="flex flex-col items-center gap-4 py-10">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl accent-gradient-soft ring-1 ring-inset ring-white/10">
              <LoaderCircle className="h-7 w-7 animate-spin text-fg" />
            </div>
            <div className="w-full max-w-[420px] space-y-2 text-center">
              <div className="text-xs font-semibold text-fg">{job.state === 'queued' ? 'Waiting for the GPU…' : job.message || 'Rendering frames…'}</div>
              <ProgressBar value={job.state === 'running' ? job.progress : null} />
              <div className="text-3xs tabular text-fg-4">{Math.round(job.progress * 100)}%</div>
            </div>
          </div>
        )
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-5 gap-2">
            {ANIMATION_KINDS.map((k) => {
              const on = req.kind === k.id
              return (
                <button
                  key={k.id}
                  type="button"
                  onClick={() => setReq((r) => ({ ...r, kind: k.id }))}
                  data-tip={k.description}
                  className={cn(
                    'flex flex-col items-center gap-2 rounded-xl border px-2 py-3 text-center transition-[border-color,background-color]',
                    on ? 'border-accent/55 bg-accent/[0.09]' : 'border-line bg-surface-0/40 hover:border-line-2',
                  )}
                >
                  <span className={cn('flex h-9 w-9 items-center justify-center rounded-xl [&>svg]:h-[18px] [&>svg]:w-[18px]', on ? 'accent-gradient text-white' : 'bg-white/[0.05] text-fg-3')}>
                    {KIND_ICON[k.id]}
                  </span>
                  <span className={cn('text-2xs font-semibold', on ? 'text-fg' : 'text-fg-2')}>{k.label}</span>
                </button>
              )
            })}
          </div>
          <p className="-mt-2 text-center text-2xs text-fg-4">{ANIMATION_KINDS.find((k) => k.id === req.kind)?.description}</p>

          <div className="grid grid-cols-2 gap-x-6 gap-y-[9px]">
            <SliderRow label="Frames" value={req.frames} min={12} max={360} step={1} decimals={0} onChange={(v) => setReq((r) => ({ ...r, frames: Math.round(v) }))} />
            <Row label="FPS">
              <Segmented size="xs" fill value={String(req.fps)} onChange={(v) => setReq((r) => ({ ...r, fps: Number(v) }))} options={['12', '24', '30', '60'].map((v) => ({ value: v, label: v }))} />
            </Row>
            <Row label="Size">
              <Segmented size="xs" fill value={String(req.size)} onChange={(v) => setReq((r) => ({ ...r, size: Number(v) }))} options={['256', '512', '768', '1024'].map((v) => ({ value: v, label: v }))} />
            </Row>
            <Row label="Format">
              <Segmented
                size="xs"
                fill
                value={req.format}
                onChange={(v) => setReq((r) => ({ ...r, format: v }))}
                options={[
                  { value: 'mp4', label: 'MP4', tip: 'H.264 video (opaque backdrop)' },
                  { value: 'webp', label: 'WebP', tip: 'Animated WebP with transparency' },
                  { value: 'gif', label: 'GIF', tip: 'Animated GIF' },
                  { value: 'png', label: 'PNG', tip: 'Transparent PNG frame sequence (.zip)' },
                ]}
              />
            </Row>
            <Row label="Quality">
              <Segmented
                size="xs"
                fill
                value={req.quality}
                onChange={(v) => setReq((r) => ({ ...r, quality: v as Quality }))}
                options={(['draft', 'preview', 'final'] as Quality[]).map((q) => ({ value: q, label: presets?.quality[q]?.label ?? q, tip: presets?.quality[q]?.description }))}
              />
            </Row>
          </div>
          {heavy && (
            <div className="rounded-lg border border-warn/25 bg-warn/[0.07] px-3 py-2 text-2xs text-warn">
              Final-quality frames above 512 px take a while ({req.frames} × Cycles renders). Draft or Preview are great for checking motion first.
            </div>
          )}
        </div>
      )}
    </Dialog>
  )
}
