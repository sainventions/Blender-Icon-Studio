// Editor surfaces for looks: the Document-tab section and the top-bar "Looks" popover.
import { useMemo, useRef, useState } from 'react'
import { ClipboardCopy, ClipboardPaste, Wand2 } from 'lucide-react'
import { activeLookId, useCopiedStyle } from '../../lib/looks'
import { cn, prettyShortcut, relativeTime } from '../../lib/format'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { Button, Popover, Section, useAnchor } from '../../components/ui'
import { LooksGrid } from './LooksGallery'
import { applyLook, copyStyle, pasteStyle, useApplyingLook } from './styleActions'

function useActiveLook(): string | null {
  const layers = useEditor((s) => s.project?.layers)
  const plateMaterial = useEditor((s) => s.project?.canvas.plate.material)
  const looks = useAppStore((s) => s.presets.data?.looks)
  return useMemo(() => activeLookId(layers, plateMaterial, looks), [layers, plateMaterial, looks])
}

function CopyPasteRow({ className }: { className?: string }) {
  const copied = useCopiedStyle()
  const busy = useEditor((s) => !!s.busy)
  return (
    <div className={cn('flex items-center gap-1.5', className)}>
      <Button size="xs" variant="subtle" icon={<ClipboardCopy />} onClick={() => void copyStyle()} tipLabel="Copy this project’s style" tipKbd="Mod+Alt+C">
        Copy style
      </Button>
      <Button
        size="xs"
        variant="subtle"
        icon={<ClipboardPaste />}
        disabled={!copied || busy}
        onClick={() => void pasteStyle()}
        tipLabel={copied ? `Paste the style copied from “${copied.sourceName}” ${relativeTime(new Date(copied.copiedAt).toISOString())}` : 'Copy a style first'}
        tipKbd="Mod+Alt+V"
        className="min-w-0"
      >
        <span className="truncate">{copied ? `Paste from “${copied.sourceName}”` : 'Paste style'}</span>
      </Button>
    </div>
  )
}

/** Top section of the Document inspector. */
export function LooksSection() {
  const presets = useAppStore((s) => s.presets.data)
  const busy = useEditor((s) => s.busy)
  const applying = useApplyingLook()
  const active = useActiveLook()
  const [hover, setHover] = useState<string | null>(null)
  const shownId = hover ?? applying ?? active
  const shown = shownId ? presets?.looks?.[shownId] : null

  return (
    <Section id="doc.looks" title="Looks" icon={<Wand2 />}>
      <div className="flex min-h-[44px] items-start gap-2 rounded-lg border border-line bg-surface-0/50 px-2.5 py-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-2xs font-semibold text-fg-2">
            {shown ? shown.label : 'Custom style'}
            {shownId && shownId === active && !hover && <span className="rounded-[4px] bg-accent/15 px-1 text-[9px] font-semibold uppercase tracking-wide text-[#c4baff]">Current</span>}
          </div>
          <div className="mt-0.5 text-3xs leading-snug text-fg-4">
            {shown
              ? shown.description
              : 'Your own mix of materials. Pick a look to restyle every layer, the plate and the lighting in one step (undoable).'}
          </div>
        </div>
      </div>
      <LooksGrid
        presets={presets}
        value={active}
        busyId={applying}
        disabled={!!busy}
        onPick={(id) => void applyLook(id)}
        onHover={setHover}
        variant="compact"
        columns={4}
      />
      <CopyPasteRow className="pt-1" />
    </Section>
  )
}

/** Top-bar "Looks" button + popover gallery. */
export function LooksButton() {
  const presets = useAppStore((s) => s.presets.data)
  const busy = useEditor((s) => s.busy)
  const applying = useApplyingLook()
  const active = useActiveLook()
  const ref = useRef<HTMLButtonElement>(null)
  const pop = useAnchor<HTMLButtonElement>()
  const activeLabel = active ? presets?.looks?.[active]?.label : null

  return (
    <>
      <Button
        ref={ref}
        variant="ghost"
        size="sm"
        icon={<Wand2 />}
        onClick={() => ref.current && pop.toggle(ref.current)}
        tipLabel={activeLabel ? `Looks · current: ${activeLabel}` : 'Looks — restyle the whole icon in one click'}
        aria-label="Looks"
        aria-haspopup="dialog"
        aria-expanded={pop.open}
        className={cn(pop.open && 'bg-white/[0.07] text-fg')}
      >
        <span className="hidden xl:inline">Looks</span>
      </Button>
      <Popover open={pop.open} onClose={pop.close} anchor={pop.anchor} placement="bottom-end" className="w-[600px] max-w-[calc(100vw-16px)]" autoFocus>
        <div className="flex items-start gap-3 border-b border-line px-4 pb-3 pt-3.5">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg accent-gradient-soft ring-1 ring-inset ring-white/10">
            <Wand2 className="h-4 w-4 text-fg" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-fg">Looks</div>
            <div className="text-2xs text-fg-3">
              Materials, depth, plate and lighting in one click — applied to every layer as a single undo step.
            </div>
          </div>
        </div>
        <div className="max-h-[min(62vh,560px)] overflow-y-auto p-2.5">
          <LooksGrid presets={presets} value={active} busyId={applying} disabled={!!busy} onPick={(id) => void applyLook(id)} columns={4} />
        </div>
        <div className="flex items-center gap-2 border-t border-line bg-surface-0/50 px-3 py-2">
          <CopyPasteRow className="min-w-0 flex-1" />
          <span className="shrink-0 text-3xs text-fg-4">
            {prettyShortcut('Mod+Alt+C')} / {prettyShortcut('Mod+Alt+V')}
          </span>
        </div>
      </Popover>
    </>
  )
}
