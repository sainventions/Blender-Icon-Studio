// Variant matrix: all six renditions side by side + a size "waterfall" of the edited appearance.
import { LoaderCircle, RefreshCw } from 'lucide-react'
import type { AppearanceId } from '../../../types'
import { cn, formatSeconds } from '../../../lib/format'
import { APPEARANCE_IDS } from '../../../lib/projectOps'
import { APPEARANCE_VISUAL } from '../../../lib/meta'
import { appearanceLabel, engineLabel } from '../../../lib/labels'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { useRender } from '../../../store/render'
import { useUi } from '../../../store/ui'
import { AppearanceIcon } from '../../../components/icons'
import { Badge, Button, Segmented } from '../../../components/ui'
import { useAppearanceImage, useRenderSig } from './RenditionsStrip'

const WATERFALL = [180, 120, 87, 60, 40, 29, 20]

export function MatrixView() {
  const appearance = useEditor((s) => s.project!.appearance)
  const platform = useEditor((s) => s.project!.canvas.platform)
  const presets = useAppStore((s) => s.presets.data)
  const allowed = presets?.platforms[platform]?.appearances ?? APPEARANCE_IDS
  const quality = useUi((s) => s.matrixQuality)
  const set = useUi((s) => s.set)
  const renderAll = useRender((s) => s.renderRenditions)
  const anyRunning = useRender((s) => Object.values(s.entries).some((e) => e.purpose === 'rendition' && (e.state === 'queued' || e.state === 'running')))
  const current = useAppearanceImage(appearance)
  const sig = useRenderSig()

  return (
    <div className="absolute inset-0 overflow-y-auto px-8 pb-8 pt-[68px]">
      <div className="mx-auto max-w-[980px]">
        <div className="mb-4 flex items-center gap-3">
          <div>
            <h3 className="text-sm font-semibold text-fg">Renditions</h3>
            <p className="text-2xs text-fg-4">
              Every appearance the OS can show — click one to edit it. {presets?.platforms[platform]?.label} uses {allowed.length} of 6.
            </p>
          </div>
          <div className="flex-1" />
          <Segmented
            size="sm"
            value={quality}
            onChange={(v) => set({ matrixQuality: v })}
            options={[
              { value: 'draft', label: 'Draft', tip: 'EEVEE — fast' },
              { value: 'preview', label: 'Preview', tip: 'Cycles OptiX — faithful glass' },
            ]}
          />
          <Button variant="primary" size="sm" icon={anyRunning ? <LoaderCircle className="animate-spin" /> : <RefreshCw />} onClick={() => void renderAll(quality)} disabled={anyRunning} tipKbd="Shift+M" tipLabel="Render all renditions">
            Render all
          </Button>
        </div>

        <div className="grid grid-cols-3 gap-3">
          {APPEARANCE_IDS.map((a, i) => (
            <MatrixTile key={a} appearance={a} index={i} active={a === appearance} supported={allowed.includes(a)} sig={sig} />
          ))}
        </div>

        <div className="mt-8">
          <div className="mb-3 flex items-baseline gap-2">
            <h3 className="text-sm font-semibold text-fg">Size waterfall</h3>
            <span className="text-2xs text-fg-4">{appearanceLabel(appearance, presets)} · actual pixels — check legibility at small sizes</span>
          </div>
          <div className="flex items-end gap-6 overflow-x-auto rounded-2xl border border-line px-6 pb-4 pt-6" style={{ background: APPEARANCE_VISUAL[appearance].bg }}>
            {WATERFALL.map((px) => (
              <div key={px} className="flex shrink-0 flex-col items-center gap-2">
                <div style={{ width: px, height: px }} className="relative">
                  {current.entry?.url ? (
                    <img src={current.entry.url} alt="" className="h-full w-full object-contain" draggable={false} />
                  ) : (
                    <div className="h-full w-full rounded-[22%] bg-black/10" />
                  )}
                </div>
                <span className="rounded-full bg-black/35 px-1.5 text-3xs tabular text-white/85">{px}px</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function MatrixTile({ appearance, index, active, supported, sig }: { appearance: AppearanceId; index: number; active: boolean; supported: boolean; sig: string }) {
  const { entry, running } = useAppearanceImage(appearance)
  const stale = !!entry?.sig && entry.sig !== sig
  const presets = useAppStore((s) => s.presets.data)
  const setAppearance = useEditor((s) => s.setAppearance)
  return (
    <button
      type="button"
      onClick={() => setAppearance(appearance)}
      onDoubleClick={() => useUi.getState().set({ stageMode: 'render' })}
      className={cn(
        'group relative aspect-square overflow-hidden rounded-2xl border text-left transition-[box-shadow,transform,border-color] duration-200',
        active ? 'border-transparent shadow-[0_0_0_2px_var(--color-accent),0_20px_50px_-20px_rgb(123_102_255/0.7)]' : 'border-line-2 hover:-translate-y-0.5 hover:border-line-3',
        !supported && 'opacity-50',
      )}
      style={{ background: APPEARANCE_VISUAL[appearance].bg }}
    >
      {entry?.url ? (
        <img src={entry.url} alt="" draggable={false} className="absolute inset-[8%] h-[84%] w-[84%] object-contain drop-shadow-[0_14px_24px_rgb(0_0_0/0.35)] transition-transform duration-300 group-hover:scale-[1.03]" />
      ) : (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2" style={{ color: APPEARANCE_VISUAL[appearance].fg }}>
          <AppearanceIcon appearance={appearance} className="h-7 w-7 opacity-60" />
          <span className="rounded-full bg-black/30 px-2 py-0.5 text-3xs text-white/80">Not rendered yet</span>
        </div>
      )}
      {running && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/30 backdrop-blur-[2px]">
          <LoaderCircle className="h-6 w-6 animate-spin text-white" />
        </div>
      )}
      <div className="absolute inset-x-2 bottom-2 flex items-center gap-1.5 rounded-lg bg-black/45 px-2 py-1 backdrop-blur">
        <span className="text-2xs font-semibold text-white">{appearanceLabel(appearance, presets)}</span>
        <span className="text-3xs text-white/50">{index + 1}</span>
        <div className="flex-1" />
        {!supported && <Badge>unused</Badge>}
        {stale && !running && <Badge tone="warn" tip="Rendered before your latest edits">outdated</Badge>}
        {entry && (
          <span className="text-3xs tabular text-white/70" data-tip={`${engineLabel(entry.engine, entry.quality)} · ${entry.width ?? '?'} px`}>
            {entry.quality} · {formatSeconds(entry.seconds)}
          </span>
        )}
      </div>
    </button>
  )
}
