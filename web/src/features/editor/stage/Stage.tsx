import { useCallback, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import {
  Box,
  Columns2,
  Grid3x3,
  Image as ImageIcon,
  LayoutGrid,
  Minus,
  Pin,
  Play,
  Plus,
  Rotate3d,
  Square,
  TriangleAlert,
  X,
} from 'lucide-react'
import type { LayerTransform } from '../../../types'
import { clamp, cn, formatElapsed, formatSeconds } from '../../../lib/format'
import { renderSig, updateLayer } from '../../../lib/projectOps'
import { useDevicePixelRatio, useElementSize, useNow } from '../../../lib/hooks'
import { appearanceLabel, engineLabel } from '../../../lib/labels'
import { checkerOverlayStyle, STAGE_BACKDROP, stageBackgroundStyle } from '../../../lib/stageBackdrop'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { activeEntry, latestFor, useRender, type RenderEntry } from '../../../store/render'
import { useUi, type StageMode } from '../../../store/ui'
import { Button, EmptyState, IconButton, Menu, Popover, ProgressRing, Segmented, Slider, SliderRow, useAnchor, type MenuItem } from '../../../components/ui'
import { LightDial, normalizeAngle, Viewport } from '../viewportBridge'
import { animateIso, setIso } from '../actions'
import { RenderImage } from './RenderImage'
import { IconGrid } from './IconGrid'
import { MatrixView } from './MatrixView'
import { actualPixelsZoom, frameSide, isOneToOne, pixelScaleLabel } from './frame'
import { RenditionsStrip } from './RenditionsStrip'

const FRAME_PAD_TOP = 64
const FRAME_PAD = 40
const STAGE_BG = stageBackgroundStyle()
const CHECKER_OVERLAY = checkerOverlayStyle()

/** Ctrl/⌘ + wheel zooms the stage. Needs a native non-passive listener: React registers `wheel` as passive, so
 *  preventDefault() in onWheel is ignored and the browser would zoom the whole app (Edge --app window) too. */
function onStageWheel(e: WheelEvent) {
  if (!(e.ctrlKey || e.metaKey)) return
  e.preventDefault()
  const z = useUi.getState().zoom
  useUi.getState().set({ zoom: clamp(Math.round(z * (e.deltaY < 0 ? 1.1 : 1 / 1.1) * 100) / 100, 0.25, 4) })
}

function wheelZoomRef(el: HTMLDivElement | null) {
  if (!el) return
  el.addEventListener('wheel', onStageWheel, { passive: false })
  return () => el.removeEventListener('wheel', onStageWheel)
}

/** Width in image pixels of the render the Render / Compare views show, when known. */
function useShownNativeWidth(mode: StageMode): number | null {
  const { entry } = useShownRender()
  return (mode === 'render' || mode === 'compare') && entry?.url && entry.width ? entry.width : null
}

export function Stage() {
  const mode = useUi((s) => s.stageMode)
  const zoom = useUi((s) => s.zoom)
  const renditionsOpen = useUi((s) => s.renditionsOpen)
  const [areaRef, area] = useElementSize<HTMLDivElement>()
  const dpr = useDevicePixelRatio()
  const native = useShownNativeWidth(mode)
  const fit = Math.max(160, Math.min(area.width - FRAME_PAD * 2, area.height - FRAME_PAD_TOP - FRAME_PAD))
  const side = frameSide(fit, zoom, dpr, native)
  const stageRef = useRef<HTMLDivElement>(null)
  const stageElement = useCallback(() => stageRef.current, [])

  return (
    <div ref={stageRef} className="relative flex min-w-0 flex-1 flex-col" style={STAGE_BG}>
      <div ref={areaRef} className="relative min-h-0 flex-1 overflow-hidden">
        {mode === 'matrix' ? (
          <MatrixView />
        ) : (
          <div ref={wheelZoomRef} className="absolute inset-0 overflow-auto">
            <div
              className="flex items-center justify-center"
              style={{
                minWidth: '100%',
                minHeight: '100%',
                width: side + FRAME_PAD * 2,
                height: side + FRAME_PAD_TOP + FRAME_PAD,
                paddingTop: FRAME_PAD_TOP - FRAME_PAD,
              }}
            >
              <PixelSnapped className="relative shrink-0" style={{ width: side, height: side }}>
                {area.width > 0 && (
                  <StageContent mode={mode} side={side} scale={native ? (side * dpr) / native : null} stageElement={stageElement} />
                )}
              </PixelSnapped>
            </div>
          </div>
        )}
        <StageToolbar fit={fit} dpr={dpr} native={native} />
        <RenderStatus />
      </div>
      {renditionsOpen ? (
        <RenditionsStrip />
      ) : (
        <button
          type="button"
          onClick={() => useUi.getState().set({ renditionsOpen: true })}
          className="absolute bottom-2 left-1/2 z-10 -translate-x-1/2 rounded-full border border-line bg-surface-2/90 px-3 py-1 text-3xs font-medium text-fg-3 backdrop-blur transition-colors hover:text-fg"
        >
          Show renditions
        </button>
      )}
    </div>
  )
}

function StageContent({
  mode,
  side,
  scale,
  stageElement,
}: {
  mode: StageMode
  side: number
  /** Screen pixels per image pixel of the shown render (null = unknown / no render). */
  scale: number | null
  stageElement: () => HTMLElement | null
}) {
  return (
    <>
      {(mode === 'viewport' || mode === 'compare') && <LiveViewport stageElement={stageElement} />}
      {mode === 'render' && <RenderPane scale={scale} />}
      {mode === 'compare' && <CompareOverlay side={side} scale={scale} stageElement={stageElement} />}
      <FrameLabel mode={mode} />
    </>
  )
}

// ------------------------------------------------------------------------------------------ live three.js
function LiveViewport({ stageElement }: { stageElement: () => HTMLElement | null }) {
  const project = useEditor((s) => s.project)!
  const geometry = useEditor((s) => s.geometry)
  const selected = useEditor((s) => s.selection.primary)
  const presets = useAppStore((s) => s.presets.data)!
  const isoAnim = useUi((s) => s.isoAnim)
  const view = useUi((s) => s.view3d)
  const showGrid = useUi((s) => s.showGrid)

  const onSelect = (id: string | null) => {
    const ed = useEditor.getState()
    if (id) {
      ed.selectLayer(id)
      useUi.getState().set({ inspectorTab: 'layer' })
    } else ed.clearSelection()
  }
  const onTransform = (id: string, t: LayerTransform) =>
    useEditor.getState().commit((p) => updateLayer(p, id, (l) => (l.locked ? l : { ...l, transform: { ...t } })), { coalesce: `drag:${id}` })

  return (
    <Viewport
      project={project}
      geometry={geometry}
      presets={presets}
      appearance={project.appearance}
      selectedLayerId={selected}
      onSelectLayer={onSelect}
      onLayerTransform={onTransform}
      iso={isoAnim ?? project.camera.iso ?? 0}
      view={view}
      showGrid={showGrid}
      stageElement={stageElement}
      className="absolute inset-0 h-full w-full"
    />
  )
}

// ------------------------------------------------------------------------------------------ Blender render
function useShownRender(): { entry: RenderEntry | null; pinned: boolean; stale: boolean } {
  const project = useEditor((s) => s.project)!
  const pinnedId = useRender((s) => s.pinned)
  const pinnedEntry = useRender((s) => (s.pinned ? (s.entries[s.pinned] ?? null) : null))
  const latest = useRender((s) => latestFor(s, project.appearance))
  const sig = useMemo(() => renderSig(project), [project])
  const entry = pinnedId ? pinnedEntry : latest
  return { entry, pinned: !!pinnedId, stale: !!entry?.sig && entry.sig !== sig && !pinnedId }
}

function RenderPane({ scale }: { scale: number | null }) {
  const { entry, pinned, stale } = useShownRender()
  const showGrid = useUi((s) => s.showGrid)
  const appearance = useEditor((s) => s.project!.appearance)
  const active = useRender((s) => activeEntry(s, appearance))
  const liveError = useRender((s) => s.liveError)
  const system = useAppStore((s) => s.system)
  const render = useRender((s) => s.render)

  if (!entry?.url) {
    if (system && !system.blender.found) {
      return (
        <Centered>
          <EmptyState icon={<TriangleAlert />} title="Blender 5.0 was not found">
            Install Blender 5.0 at the default location (or set its path on the server) to get EEVEE and Cycles renders. The live
            three.js viewport keeps working meanwhile.
          </EmptyState>
        </Centered>
      )
    }
    return (
      <Centered>
        {active ? (
          <div className="flex flex-col items-center gap-3">
            <ProgressRing value={active.state === 'running' ? active.progress : null} size={44} stroke={3} />
            <div className="text-xs font-medium text-fg-2">Rendering {active.quality}…</div>
            <div className="text-2xs text-fg-4">{active.message}</div>
          </div>
        ) : (
          <EmptyState
            icon={<ImageIcon />}
            title="No Blender render yet"
            action={
              <Button variant="primary" size="sm" icon={<Play />} onClick={() => void render('preview')} tipKbd="R" tipLabel="Cycles preview">
                Render preview
              </Button>
            }
          >
            {liveError ? <span className="text-bad">{liveError}</span> : 'Drafts render automatically after edits; Cycles previews follow once things settle.'}
          </EmptyState>
        )}
      </Centered>
    )
  }
  return (
    <div className="absolute inset-0">
      <div className="absolute inset-0" style={CHECKER_OVERLAY} />
      <RenderImage url={entry.url} className="absolute inset-0" pixelExact={isOneToOne(scale)} />
      {showGrid && <IconGrid className="pointer-events-none absolute inset-0 h-full w-full" />}
      <RenderInfoChip entry={entry} pinned={pinned} stale={stale} scale={scale} />
    </div>
  )
}

/**
 * Keeps its box on whole device pixels (a centred frame can land on a half pixel, which resamples a 1:1 render and
 * softens it): measures the untransformed position and translates by the sub-pixel remainder.
 */
function PixelSnapped({ className, style, children }: { className?: string; style?: CSSProperties; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const dpr = useDevicePixelRatio()
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    let raf = 0
    const snap = () => {
      raf = 0
      el.style.transform = ''
      const r = el.getBoundingClientRect()
      const dx = Math.round(r.left * dpr) / dpr - r.left
      const dy = Math.round(r.top * dpr) / dpr - r.top
      el.style.transform = Math.abs(dx) > 1e-3 || Math.abs(dy) > 1e-3 ? `translate(${dx}px, ${dy}px)` : ''
    }
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(snap)
    }
    snap()
    const ro = new ResizeObserver(schedule)
    ro.observe(el)
    if (el.parentElement) ro.observe(el.parentElement)
    window.addEventListener('resize', schedule)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      ro.disconnect()
      window.removeEventListener('resize', schedule)
    }
  }, [dpr])
  return (
    <div ref={ref} className={className} style={style}>
      {children}
    </div>
  )
}

function RenderInfoChip({ entry, pinned, stale, scale }: { entry: RenderEntry; pinned: boolean; stale: boolean; scale: number | null }) {
  const one = isOneToOne(scale)
  return (
    <div className="absolute -bottom-9 left-1/2 flex -translate-x-1/2 items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-surface-2/85 px-2.5 py-1 text-3xs text-fg-3 backdrop-blur">
      {pinned && <Pin className="h-3 w-3 text-accent" />}
      <span className="font-semibold capitalize text-fg-2">{entry.quality}</span>
      <span>·</span>
      <span>{engineLabel(entry.engine, entry.quality)}</span>
      {entry.width && (
        <>
          <span>·</span>
          <span className="tabular">{entry.width}px</span>
        </>
      )}
      {entry.seconds != null && (
        <>
          <span>·</span>
          <span className="tabular">{formatSeconds(entry.seconds)}</span>
        </>
      )}
      {scale != null && (
        <span
          data-testid="pixel-scale"
          data-tip={
            one
              ? 'Actual pixels: one render pixel per screen pixel'
              : scale < 1
                ? `Shown at ${Math.round(scale * 100)}% of the render's pixels`
                : `Zoomed past the render's resolution (${Math.round(scale * 100)}%) — render larger for more detail`
          }
          className={cn(
            'ml-0.5 rounded-[4px] px-1 tabular leading-[14px] ring-1 ring-inset',
            one ? 'text-fg-2 ring-white/15' : scale > 1 ? 'text-warn/90 ring-warn/25' : 'text-fg-4 ring-white/10',
          )}
        >
          {pixelScaleLabel(scale)}
        </span>
      )}
      {stale && <span className="ml-1 rounded-full bg-warn/15 px-1.5 text-warn">outdated</span>}
      {pinned && (
        <button type="button" onClick={() => useRender.getState().pin(null)} className="ml-1 rounded-full bg-white/10 px-1.5 text-fg-2 hover:bg-white/20">
          Back to latest
        </button>
      )}
    </div>
  )
}

function CompareOverlay({ side, scale, stageElement }: { side: number; scale: number | null; stageElement: () => HTMLElement | null }) {
  const split = useUi((s) => s.compareSplit)
  const set = useUi((s) => s.set)
  const { entry, stale } = useShownRender()
  const dragging = useRef(false)
  const [hover, setHover] = useState(false)
  const x = split * side

  return (
    <div className="pointer-events-none absolute inset-0">
      {entry?.url ? (
        <div className="absolute inset-0" style={{ clipPath: `inset(0 0 0 ${x}px)` }}>
          <StageBackdropFill stageElement={stageElement} />
          <RenderImage url={entry.url} className="absolute inset-0" pixelExact={isOneToOne(scale)} />
        </div>
      ) : (
        <div className="absolute inset-y-0 right-0 flex items-center justify-center bg-black/30 text-2xs text-fg-3 backdrop-blur-sm" style={{ left: x }}>
          <span className="px-4 text-center">No Blender render yet</span>
        </div>
      )}
      <span className="absolute left-2 top-2 rounded-full bg-black/45 px-2 py-0.5 text-3xs font-semibold text-fg-2 backdrop-blur">three.js live</span>
      <span className="absolute right-2 top-2 rounded-full bg-black/45 px-2 py-0.5 text-3xs font-semibold text-fg-2 backdrop-blur">
        Blender {entry ? `· ${entry.quality}` : ''}
        {stale ? ' · outdated' : ''}
      </span>
      {/* divider */}
      <div
        className="pointer-events-auto absolute inset-y-0 z-10 w-5 -translate-x-1/2 cursor-ew-resize"
        style={{ left: x }}
        onPointerEnter={() => setHover(true)}
        onPointerLeave={() => setHover(false)}
        onPointerDown={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
          dragging.current = true
        }}
        onPointerMove={(e) => {
          if (!dragging.current) return
          const parent = (e.currentTarget as HTMLElement).parentElement!.getBoundingClientRect()
          set({ compareSplit: clamp((e.clientX - parent.left) / parent.width, 0, 1) })
        }}
        onPointerUp={() => (dragging.current = false)}
        onDoubleClick={() => set({ compareSplit: 0.5 })}
      >
        <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-white/80 shadow-[0_0_10px_rgb(0_0_0/0.6)]" />
        <div
          className={cn(
            'absolute left-1/2 top-1/2 flex h-8 w-8 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/60 bg-black/50 text-white shadow-pop backdrop-blur transition-transform',
            hover && 'scale-110',
          )}
        >
          <Columns2 className="h-3.5 w-3.5" />
        </div>
      </div>
    </div>
  )
}

/**
 * Opaque copy of what is behind the frame — the stage background (aligned to the stage element, so its glow and dot
 * grid line up) under the faded checkerboard — for the render side of Compare: it hides the live view beneath and
 * matches the live view's own backdrop, so neither half shows a box.
 */
function StageBackdropFill({ stageElement }: { stageElement: () => HTMLElement | null }) {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    let raf = 0
    const update = () => {
      raf = 0
      const st = stageElement()
      if (!st) return
      const a = el.getBoundingClientRect()
      const b = st.getBoundingClientRect()
      const x = b.left - a.left
      const y = b.top - a.top
      el.style.backgroundSize = `${b.width}px ${b.height}px, ${STAGE_BACKDROP.dots.spacing}px ${STAGE_BACKDROP.dots.spacing}px`
      el.style.backgroundPosition = `${x}px ${y}px, ${x}px ${y}px`
    }
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(update)
    }
    update()
    const ro = new ResizeObserver(schedule)
    ro.observe(el)
    const st = stageElement()
    if (st) ro.observe(st)
    window.addEventListener('resize', schedule)
    document.addEventListener('scroll', schedule, { capture: true, passive: true })
    return () => {
      if (raf) cancelAnimationFrame(raf)
      ro.disconnect()
      window.removeEventListener('resize', schedule)
      document.removeEventListener('scroll', schedule, { capture: true })
    }
  }, [stageElement])
  return (
    <div ref={ref} className="absolute inset-0" style={{ ...STAGE_BG, backgroundRepeat: 'no-repeat, repeat' }}>
      <div className="absolute inset-0" style={CHECKER_OVERLAY} />
    </div>
  )
}

function FrameLabel({ mode }: { mode: StageMode }) {
  const appearance = useEditor((s) => s.project!.appearance)
  const presets = useAppStore((s) => s.presets.data)
  if (mode === 'render') return null
  return (
    <div className="pointer-events-none absolute -bottom-9 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full border border-line bg-surface-2/85 px-2.5 py-1 text-3xs text-fg-3 backdrop-blur">
      {mode === 'compare' ? 'Drag the divider to compare' : `Live preview · ${appearanceLabel(appearance, presets)}`}
    </div>
  )
}

function Centered({ children }: { children: ReactNode }) {
  return <div className="absolute inset-0 flex items-center justify-center rounded-[22%] border border-dashed border-line-2 bg-white/[0.015]">{children}</div>
}

// ------------------------------------------------------------------------------------------ floating toolbar
const ZOOMS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4]

function StageToolbar({ fit, dpr, native }: { fit: number; dpr: number; native: number | null }) {
  const mode = useUi((s) => s.stageMode)
  const showGrid = useUi((s) => s.showGrid)
  const zoom = useUi((s) => s.zoom)
  const set = useUi((s) => s.set)
  const angle = useEditor((s) => s.project!.lighting.angle)
  const lightingPreset = useEditor((s) => s.project!.lighting.preset)
  const lockAngle = useAppStore((s) => s.presets.data?.lighting[lightingPreset]?.lockAngle)
  const zoomMenu = useAnchor<HTMLButtonElement>()
  const zoomRef = useRef<HTMLButtonElement>(null)
  const actual = native ? +actualPixelsZoom(fit, dpr, native).toFixed(4) : null
  const zoomItems: MenuItem[] = [
    ...ZOOMS.map<MenuItem>((z) => ({
      key: String(z),
      label: z === 1 ? (native ? 'Fit (100%, max 1:1)' : 'Fit (100%)') : `${z * 100}%`,
      checked: zoom === z,
      onSelect: () => set({ zoom: z }),
    })),
    ...(actual != null && Math.abs(actual - 1) > 0.001
      ? ([
          { type: 'separator', key: 'sep-actual' },
          { key: 'actual', label: 'Actual pixels (1:1)', checked: Math.abs(zoom - actual) < 0.001, onSelect: () => set({ zoom: actual }) },
        ] satisfies MenuItem[])
      : []),
  ]

  const setAngle = (deg: number) => {
    const a = normalizeAngle(deg)
    useEditor.getState().commit((p) => (p.lighting.angle === a ? p : { ...p, lighting: { ...p.lighting, angle: a } }), { coalesce: 'lighting.angle' })
  }

  return (
    <div className="absolute left-1/2 top-3 z-20 flex -translate-x-1/2 items-center gap-1 rounded-xl glass px-1.5 py-1 shadow-panel">
      <Segmented<StageMode>
        size="sm"
        value={mode}
        onChange={(v) => set({ stageMode: v })}
        options={[
          { value: 'viewport', icon: <Rotate3d />, label: 'Live', tip: 'Live three.js viewport', kbd: 'V' },
          { value: 'render', icon: <ImageIcon />, label: 'Render', tip: 'Latest Blender render', kbd: 'V' },
          { value: 'compare', icon: <Columns2 />, label: 'Compare', tip: 'Split compare: live vs Blender', kbd: 'V' },
          { value: 'matrix', icon: <LayoutGrid />, label: 'Matrix', tip: 'All six renditions + size waterfall', kbd: 'V' },
        ]}
      />
      {/* Matrix shows finished renders (always head-on): view angle, zoom, grid and the light dial do nothing there. */}
      {mode !== 'matrix' && (
        <>
          <Divider />
          <ViewAngleControl />
          <Divider />
          <IconButton label="Icon grid" kbd="G" active={showGrid} onClick={() => set({ showGrid: !showGrid })}>
            <Grid3x3 />
          </IconButton>
          <div className="flex items-center">
            <IconButton label="Zoom out" size="sm" onClick={() => set({ zoom: clamp(+(zoom / 1.25).toFixed(2), 0.25, 4) })}>
              <Minus />
            </IconButton>
            <button
              ref={zoomRef}
              type="button"
              onClick={() => zoomRef.current && zoomMenu.toggle(zoomRef.current)}
              className="h-6 w-11 rounded-md text-2xs tabular text-fg-2 transition-colors hover:bg-white/[0.07]"
              data-tip="Zoom (Ctrl+wheel)"
              data-tip-kbd="Mod+0"
            >
              {Math.round(zoom * 100)}%
            </button>
            <IconButton label="Zoom in" size="sm" onClick={() => set({ zoom: clamp(+(zoom * 1.25).toFixed(2), 0.25, 4) })}>
              <Plus />
            </IconButton>
          </div>
          <Divider />
          <LightControl angle={angle} locked={lockAngle != null} onChange={setAngle} />
        </>
      )}
      <Menu
        open={zoomMenu.open && mode !== 'matrix'}
        onClose={zoomMenu.close}
        anchor={zoomMenu.anchor}
        placement="bottom"
        width={176}
        items={zoomItems}
      />
    </div>
  )
}

/**
 * View angle (PLAN §11 View): a CAD-style POV between head-on and isometric with the REAL layer distances — bound to
 * project.camera.iso, so the live view and every Blender render use the same camera. Front / Iso swing there.
 */
function ViewAngleControl() {
  const projectIso = useEditor((s) => s.project!.camera.iso ?? 0)
  const perspective = useEditor((s) => s.project!.camera.view === 'perspective')
  const isoAnim = useUi((s) => s.isoAnim)
  const iso = isoAnim ?? projectIso
  return (
    <div
      className={cn('flex items-center gap-1 px-0.5', perspective && 'opacity-60')}
      data-testid="view-angle"
      data-tip={perspective ? 'The render camera is in perspective — moving this switches it back to the CAD view' : undefined}
    >
      <IconButton label="Front view (head-on)" kbd="I" size="sm" active={iso < 0.005 && !perspective} onClick={() => animateIso(0)}>
        <Square />
      </IconButton>
      <div className="flex items-center" data-tip="View angle: head-on ↔ isometric (real layer distances, like a CAD view)" data-tip-kbd="I">
        <Slider value={iso} min={0} max={1} step={0.01} defaultValue={0} onChange={(v) => setIso(v)} className="w-20" ariaLabel="View angle" />
      </div>
      <IconButton label="Isometric view" kbd="I" size="sm" active={iso > 0.995 && !perspective} onClick={() => animateIso(1)}>
        <Box />
      </IconButton>
    </div>
  )
}

/** Toolbar light control: a compact sun button; the full dial (D2's LightDial) lives in a popover. */
function LightControl({ angle, locked, onChange }: { angle: number; locked: boolean; onChange: (deg: number) => void }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const elevation = useEditor((s) => s.project!.lighting.elevation)
  const setElevation = (v: number) =>
    useEditor.getState().commit((p) => ({ ...p, lighting: { ...p.lighting, elevation: v } }), { coalesce: 'lighting.elevation' })
  return (
    <>
      <button
        ref={ref}
        type="button"
        onClick={() => setOpen((o) => !o)}
        data-tip={locked ? 'Light angle (locked by the lighting preset)' : 'Light angle'}
        className={cn(
          'flex h-7 items-center gap-1.5 rounded-md pl-1 pr-2 text-2xs tabular text-fg-2 transition-colors hover:bg-white/[0.07]',
          open && 'bg-white/[0.07]',
          locked && 'opacity-60',
        )}
      >
        <span className="relative flex h-5 w-5 items-center justify-center rounded-full border border-line-2 bg-surface-0">
          <span className="absolute inset-0" style={{ transform: `rotate(${angle}deg)` }}>
            <span className="absolute left-1/2 top-[1px] h-[7px] w-[7px] -translate-x-1/2 rounded-full bg-[#ffd27a] shadow-[0_0_6px_rgb(255_210_122/0.9)]" />
          </span>
        </span>
        {Math.round(angle)}°
      </button>
      <Popover open={open} onClose={() => setOpen(false)} anchor={ref.current} placement="bottom" className="w-[220px] p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-2xs font-semibold text-fg-2">Light direction</span>
          <span className="text-2xs tabular text-fg-3">{Math.round(angle)}°</span>
        </div>
        <div className={cn('flex justify-center', locked && 'pointer-events-none opacity-50')}>
          <LightDial angle={angle} onChange={onChange} size={150} />
        </div>
        {locked && <p className="mt-1 text-center text-3xs text-fg-4">The lighting preset locks the angle.</p>}
        <div className="mt-3">
          <SliderRow label="Elevation" labelWidth={64} value={elevation} min={0} max={90} step={1} decimals={0} unit="°" defaultValue={50} onChange={(v) => setElevation(v)} />
        </div>
      </Popover>
    </>
  )
}

function Divider() {
  return <span className="mx-0.5 h-4 w-px bg-line-2" />
}

// ------------------------------------------------------------------------------------------ render progress
function RenderStatus() {
  const active = useRender((s) => activeEntry(s))
  const presets = useAppStore((s) => s.presets.data)
  const cancel = useRender((s) => s.cancel)
  const now = useNow(!!active, 100)
  if (!active) return null
  const started = active.startedAt ?? active.createdAt
  return (
    <div className="absolute right-3 top-3 z-20 flex items-center gap-2 rounded-xl glass py-1 pl-1.5 pr-1 shadow-panel animate-pop-in">
      <ProgressRing value={active.state === 'running' && active.progress > 0 ? active.progress : null} size={22} stroke={2.5} />
      <div className="leading-tight">
        <div className="text-2xs font-semibold capitalize text-fg">
          {active.quality} {active.state === 'queued' ? '· queued' : ''}
        </div>
        <div className="max-w-[160px] truncate text-3xs text-fg-3">
          {active.state === 'running' ? formatElapsed(now - started) : appearanceLabel(active.appearance, presets)}
          {active.message && active.state === 'running' ? ` · ${active.message}` : ''}
        </div>
      </div>
      <IconButton label="Cancel render" size="xs" onClick={() => void cancel(active.jobId)}>
        <X />
      </IconButton>
    </div>
  )
}
