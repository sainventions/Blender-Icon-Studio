import { useMemo } from 'react'
import { Box, Download, History, Image as ImageIcon, Paintbrush, Play, RotateCcw, Sparkles, Zap } from 'lucide-react'
import type { Project, Quality } from '../../../types'
import { errorMessage, systemApi } from '../../../api'
import { cn, formatSeconds, relativeTime } from '../../../lib/format'
import { appearanceLabel, engineLabel } from '../../../lib/labels'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { useRender } from '../../../store/render'
import { toast } from '../../../store/toasts'
import { useUi } from '../../../store/ui'
import { Button, ColorField, EmptyState, Row, Section, Segmented, Select, Switch } from '../../../components/ui'
import { openInBlender } from '../actions'

const TIERS: Quality[] = ['draft', 'preview', 'final', 'ultra']

export function RenderInspector() {
  const project = useEditor((s) => s.project)!
  const commit = useEditor((s) => s.commit)
  const presets = useAppStore((s) => s.presets.data)!
  const system = useAppStore((s) => s.system)
  const render = useRender((s) => s.render)
  const r = project.render
  const setRender = (key: string, patch: Partial<Project['render']>, opts: { render?: boolean } = {}) =>
    commit((p) => ({ ...p, render: { ...p.render, ...patch } }), { coalesce: `render.${key}`, render: opts.render ?? true })
  const tier = presets.quality[r.quality]

  return (
    <div className="pb-6">
      <Section id="render.output" title="Render" icon={<Sparkles />}>
        <div className="grid grid-cols-2 gap-1.5">
          {TIERS.map((q) => {
            const spec = presets.quality[q]
            const active = r.quality === q
            return (
              <button
                key={q}
                type="button"
                onClick={() => setRender('quality', { quality: q }, { render: false })}
                className={cn(
                  'rounded-lg border p-2 text-left transition-[border-color,background-color,box-shadow]',
                  active ? 'border-accent/60 bg-accent/10 shadow-[0_0_0_1px_rgb(143_125_255/0.35)]' : 'border-line bg-surface-0/50 hover:border-line-2',
                )}
              >
                <div className="flex items-center gap-1.5">
                  {spec?.engine === 'eevee' ? <Zap className="h-3 w-3 text-accent-2" /> : <Sparkles className="h-3 w-3 text-accent" />}
                  <span className="text-2xs font-semibold text-fg">{spec?.label ?? q}</span>
                </div>
                <div className="mt-0.5 text-3xs tabular text-fg-4">
                  {spec?.engine === 'eevee' ? 'EEVEE' : 'Cycles OptiX'} · {spec?.size}px{spec?.engine === 'cycles' ? ` · ${spec.samples} spp` : ''}
                </div>
              </button>
            )
          })}
        </div>
        {tier && <p className="text-3xs leading-snug text-fg-4">{tier.description}</p>}
        <Row label="Size">
          <Select
            value={String(r.size ?? 'tier')}
            onChange={(v) => setRender('size', { size: v === 'tier' ? null : Number(v) }, { render: false })}
            options={[
              { value: 'tier', label: `Tier default (${tier?.size ?? '—'} px)` },
              ...[256, 512, 768, 1024, 2048, 4096].map((s) => ({ value: String(s), label: `${s} px` })),
            ]}
            className="flex-1"
          />
        </Row>
        <Button
          variant="primary"
          size="md"
          className="w-full"
          icon={<Play className="fill-current" />}
          onClick={() => void render(r.quality, { size: r.size ?? undefined })}
        >
          Render {tier?.label ?? r.quality}
        </Button>
        {(r.quality === 'final' || r.quality === 'ultra') && (
          <p className="text-3xs leading-snug text-fg-4">Final and Ultra run in a separate Blender process (cancel frees the GPU instantly).</p>
        )}
      </Section>

      <Section id="render.look" title="Look" icon={<Paintbrush />}>
        <Row label="Colour" hint="View transform. Neutral (Khronos PBR) keeps brand colours accurate.">
          <Select
            value={r.colorMode}
            onChange={(v) => setRender('colorMode', { colorMode: v as Project['render']['colorMode'] })}
            options={Object.entries(presets.colorModes).map(([id, m]) => ({ value: id, label: m.label, description: `${m.viewTransform}${m.look !== 'None' ? ` · ${m.look}` : ''}` }))}
            className="flex-1"
          />
        </Row>
        <Row label="Backdrop">
          <Segmented
            size="xs"
            fill
            value={r.backdrop}
            onChange={(v) => setRender('backdrop', { backdrop: v })}
            options={[
              { value: 'transparent', label: 'Transparent' },
              { value: 'color', label: 'Colour' },
              { value: 'wallpaper', label: 'Wallpaper' },
            ]}
          />
        </Row>
        {r.backdrop === 'color' && (
          <Row label="Backdrop colour">
            <ColorField value={r.backdropColor} onChange={(c) => setRender('backdropColor', { backdropColor: c })} className="flex-1" />
          </Row>
        )}
      </Section>

      <Section id="render.live" title="Live rendering" icon={<Zap />}>
        <Row label="Auto preview" hint="Cycles OptiX preview (512 px, 48 spp) ~1.2 s after edits settle.">
          <Switch checked={r.autoPreview} onChange={(v) => setRender('autoPreview', { autoPreview: v }, { render: false })} label="Auto preview" />
          <span className="text-3xs text-fg-4">{r.autoPreview ? 'Cycles after edits settle' : 'EEVEE drafts only'}</span>
        </Row>
        <p className="text-3xs leading-snug text-fg-4">
          Drafts (EEVEE, ~0.2 s) render automatically ~350 ms after every edit. EEVEE can’t show glass behind glass — the Cycles preview is the first faithful view.
        </p>
      </Section>

      <Section id="render.history" title="Recent renders" icon={<History />}>
        <RenderHistory />
      </Section>

      <Section id="render.blender" title="Blender" icon={<Box />} defaultCollapsed>
        <div className="space-y-1 text-2xs text-fg-3">
          <div className="flex justify-between">
            <span>Version</span>
            <span className="text-fg-2">{system?.blender.version ?? '—'}</span>
          </div>
          <div className="flex justify-between">
            <span>Device</span>
            <span className="text-fg-2">{system?.gpu.device ?? '—'} · {system?.gpu.name ?? '—'}</span>
          </div>
          <div className="flex justify-between">
            <span>Worker</span>
            <span className="capitalize text-fg-2">{system?.worker.state ?? '—'}{system?.worker.pid ? ` · pid ${system.worker.pid}` : ''}</span>
          </div>
        </div>
        <div className="flex gap-1.5 pt-1">
          <Button size="sm" variant="secondary" icon={<Box />} onClick={() => void openInBlender()} className="flex-1">
            Open in Blender
          </Button>
          <Button
            size="sm"
            variant="secondary"
            icon={<RotateCcw />}
            onClick={async () => {
              try {
                await systemApi.restartWorker()
                toast.info('Restarting Blender worker…')
              } catch (e) {
                toast.error('Restart failed', { description: errorMessage(e) })
              }
            }}
          >
            Restart
          </Button>
        </div>
      </Section>
    </div>
  )
}

function RenderHistory() {
  const presets = useAppStore((s) => s.presets.data)
  const entries = useRender((s) => s.entries)
  const pinned = useRender((s) => s.pinned)
  const pin = useRender((s) => s.pin)
  const list = useMemo(
    () =>
      Object.values(entries)
        .filter((e) => e.state === 'done' && e.url)
        .sort((a, b) => (b.finishedAt ?? b.createdAt) - (a.finishedAt ?? a.createdAt))
        .slice(0, 14),
    [entries],
  )
  if (!list.length) return <EmptyState icon={<ImageIcon />} title="No renders yet" className="py-4" />
  return (
    <div className="space-y-1">
      {list.map((e) => (
        <div
          key={e.jobId}
          role="button"
          tabIndex={0}
          onClick={() => {
            pin(e.jobId)
            useUi.getState().set({ stageMode: 'render' })
          }}
          className={cn(
            'group flex items-center gap-2 rounded-md p-1 pr-1.5 transition-colors',
            pinned === e.jobId ? 'bg-accent/12' : 'hover:bg-white/[0.04]',
          )}
        >
          <img src={e.url} alt="" className="h-9 w-9 shrink-0 rounded-md border border-line object-contain checkerboard-sm" loading="lazy" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1 text-2xs">
              <span className="font-semibold capitalize text-fg-2">{e.quality}</span>
              <span className="text-fg-4">· {appearanceLabel(e.appearance, presets)}</span>
            </div>
            <div className="text-3xs tabular text-fg-4">
              {engineLabel(e.engine, e.quality)} · {e.width ?? '?'}px · {formatSeconds(e.seconds)} · {relativeTime(new Date(e.finishedAt ?? e.createdAt).toISOString())}
            </div>
          </div>
          <a
            href={e.url}
            download=""
            onClick={(ev) => ev.stopPropagation()}
            data-tip="Download PNG"
            className="flex h-6 w-6 items-center justify-center rounded text-fg-4 opacity-0 transition-opacity hover:bg-white/10 hover:text-fg group-hover:opacity-100"
          >
            <Download className="h-3.5 w-3.5" />
          </a>
        </div>
      ))}
    </div>
  )
}
