import { ChevronDown, LoaderCircle, RefreshCw } from 'lucide-react'
import { useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import type { AppearanceId } from '../../../types'
import { cn } from '../../../lib/format'
import { APPEARANCE_IDS, renderSig } from '../../../lib/projectOps'
import { APPEARANCE_VISUAL } from '../../../lib/meta'
import { appearanceLabel } from '../../../lib/labels'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { useRender, type RenderEntry } from '../../../store/render'
import { useUi } from '../../../store/ui'
import { AppearanceIcon } from '../../../components/icons'
import { Button, IconButton } from '../../../components/ui'

/** Best image for an appearance: the newer of its latest rendition and its latest render. */
export function useAppearanceImage(a: AppearanceId): { entry: RenderEntry | null; running: boolean } {
  return useRender(
    useShallow((s) => {
    const r = s.renditions[a] ? s.entries[s.renditions[a]!] : undefined
    const l = s.latest[a] ? s.entries[s.latest[a]!] : undefined
    const entry = !r ? (l ?? null) : !l ? r : r.createdAt >= l.createdAt ? r : l
    let running = false
    for (const e of Object.values(s.entries)) {
      if (e.appearance === a && e.purpose === 'rendition' && (e.state === 'queued' || e.state === 'running')) {
        running = true
        break
      }
    }
    return { entry, running }
    }),
  )
}

/** Render signature of the current project (to flag renders made before the latest edits). */
export function useRenderSig(): string {
  const project = useEditor((s) => s.project)
  return useMemo(() => (project ? renderSig(project) : ''), [project])
}

export function RenditionsStrip() {
  const appearance = useEditor((s) => s.project!.appearance)
  const platform = useEditor((s) => s.project!.canvas.platform)
  const presets = useAppStore((s) => s.presets.data)
  const allowed = presets?.platforms[platform]?.appearances ?? APPEARANCE_IDS
  const quality = useUi((s) => s.matrixQuality)
  const renderAll = useRender((s) => s.renderRenditions)
  const anyRunning = useRender((s) => Object.values(s.entries).some((e) => e.purpose === 'rendition' && (e.state === 'queued' || e.state === 'running')))
  const sig = useRenderSig()

  return (
    <div className="relative z-10 flex h-[86px] shrink-0 items-center gap-3 border-t border-line bg-surface-1/80 px-3 backdrop-blur">
      <div className="flex w-[92px] shrink-0 flex-col gap-1.5">
        <span className="text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">Renditions</span>
        <Button
          size="xs"
          variant="subtle"
          icon={anyRunning ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}
          onClick={() => void renderAll(quality)}
          disabled={anyRunning}
          tipLabel={`Render all appearances (${quality})`}
          tipKbd="Shift+M"
        >
          Render all
        </Button>
      </div>
      <div className="no-scrollbar flex min-w-0 flex-1 items-center justify-center gap-2 overflow-x-auto">
        {APPEARANCE_IDS.map((a, i) => (
          <RenditionTile key={a} appearance={a} index={i} active={a === appearance} supported={allowed.includes(a)} label={appearanceLabel(a, presets, 'short')} long={appearanceLabel(a, presets)} sig={sig} />
        ))}
      </div>
      <IconButton label="Hide renditions" size="sm" onClick={() => useUi.getState().set({ renditionsOpen: false })}>
        <ChevronDown />
      </IconButton>
    </div>
  )
}

function RenditionTile({
  appearance,
  index,
  active,
  supported,
  label,
  long,
  sig,
}: {
  appearance: AppearanceId
  index: number
  active: boolean
  supported: boolean
  label: string
  /** Full name for the tooltip / accessible name. */
  long: string
  sig: string
}) {
  const { entry, running } = useAppearanceImage(appearance)
  const stale = !!entry?.sig && entry.sig !== sig && !running
  const setAppearance = useEditor((s) => s.setAppearance)
  return (
    <button
      type="button"
      onClick={() => setAppearance(appearance)}
      data-tip={supported ? `Edit ${long}` : `${long} — not used by this platform`}
      aria-label={`${long} appearance`}
      aria-pressed={active}
      data-tip-kbd={String(index + 1)}
      className={cn('group flex shrink-0 flex-col items-center gap-1', !supported && 'opacity-40')}
    >
      <div
        className={cn(
          'relative h-[52px] w-[52px] overflow-hidden rounded-[12px] border transition-[box-shadow,transform,border-color] duration-200',
          active ? 'border-transparent shadow-[0_0_0_2px_var(--color-accent),0_6px_20px_-6px_rgb(123_102_255/0.8)]' : 'border-line-2 group-hover:-translate-y-0.5 group-hover:border-line-3',
        )}
        style={{ background: APPEARANCE_VISUAL[appearance].bg }}
      >
        {entry?.url ? (
          <img src={entry.url} alt="" draggable={false} className="absolute inset-0 h-full w-full object-contain p-0.5" />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center" style={{ color: APPEARANCE_VISUAL[appearance].fg }}>
            <AppearanceIcon appearance={appearance} className="h-4 w-4 opacity-70" />
          </div>
        )}
        {running && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/35">
            <LoaderCircle className="h-4 w-4 animate-spin text-white" />
          </div>
        )}
        {stale && <span className="absolute left-1 top-1 h-1.5 w-1.5 rounded-full bg-warn shadow-[0_0_4px_rgb(245_185_66/0.9)]" data-tip="Outdated — rendered before your latest edits" />}
      </div>
      <span className={cn('text-3xs font-medium', active ? 'text-fg' : 'text-fg-4 group-hover:text-fg-2')}>{label}</span>
    </button>
  )
}
