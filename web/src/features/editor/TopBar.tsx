import { useEffect, useRef, useState } from 'react'
import {
  Box,
  Check,
  ChevronDown,
  CircleAlert,
  Clapperboard,
  Download,
  LoaderCircle,
  Orbit,
  PanelLeft,
  PanelRight,
  Play,
  Redo2,
  ScanLine,
  Undo2,
} from 'lucide-react'
import type { AppearanceId, Quality } from '../../types'
import { cn, formatSeconds, relativeTime } from '../../lib/format'
import { goHome } from '../../lib/route'
import { APPEARANCE_IDS } from '../../lib/projectOps'
import { APPEARANCE_VISUAL } from '../../lib/meta'
import { useNow } from '../../lib/hooks'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { useRender } from '../../store/render'
import { useUi } from '../../store/ui'
import { AppearanceIcon, Logo, PlatformIcon } from '../../components/icons'
import { Button, IconButton, Menu, Segmented, useAnchor, type MenuItem } from '../../components/ui'
import { openInBlender, PLATFORM_IDS, setPlatform } from './actions'

export function TopBar() {
  return (
    <header className="relative z-20 flex h-11 shrink-0 items-center gap-2 border-b border-line bg-surface-1/95 px-2 backdrop-blur">
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        <button
          type="button"
          onClick={goHome}
          data-tip="All projects"
          aria-label="All projects"
          className="flex h-7 items-center gap-1.5 rounded-md px-1.5 transition-colors hover:bg-white/[0.06]"
        >
          <Logo size={20} />
        </button>
        <span className="h-4 w-px bg-line-2" />
        <ProjectName />
        <SaveIndicator />
        <span className="mx-1 h-4 w-px bg-line-2" />
        <UndoRedo />
      </div>

      <AppearanceSwitcher />

      <div className="flex min-w-0 flex-1 items-center justify-end gap-1.5">
        <PlatformPicker />
        <ViewToggle />
        <span className="mx-0.5 h-4 w-px bg-line-2" />
        <RenderButton />
        <Button
          variant="ghost"
          size="sm"
          icon={<Clapperboard />}
          onClick={() => useUi.getState().openDialog('animate')}
          tipLabel="Animate — turntable, tilt, light sweep…"
          aria-label="Animate"
        >
          <span className="hidden xl:inline">Animate</span>
        </Button>
        <Button variant="ghost" size="sm" icon={<Box />} onClick={() => void openInBlender()} tipLabel="Open the scene in Blender 5.0" aria-label="Open in Blender">
          <span className="hidden xl:inline">Blender</span>
        </Button>
        <Button variant="primary" size="sm" icon={<Download />} onClick={() => useUi.getState().openDialog('export')} tipLabel="Export icons" tipKbd="E">
          Export
        </Button>
        <span className="mx-0.5 h-4 w-px bg-line-2" />
        <PanelToggles />
      </div>
    </header>
  )
}

function ProjectName() {
  const name = useEditor((s) => s.project?.name ?? '')
  const rename = useEditor((s) => s.rename)
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(name)
  const inputRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (!editing) setText(name)
  }, [name, editing])
  useEffect(() => {
    if (editing) requestAnimationFrame(() => inputRef.current?.select())
  }, [editing])

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          rename(text)
          setEditing(false)
        }}
        onKeyDown={(e) => {
          e.stopPropagation()
          if (e.key === 'Enter') {
            rename(text)
            setEditing(false)
          } else if (e.key === 'Escape') setEditing(false)
        }}
        className="h-7 w-[220px] rounded-md border border-accent/60 bg-surface-0 px-2 text-xs font-semibold text-fg outline-none"
      />
    )
  }
  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      data-tip="Rename project"
      className="h-7 max-w-[260px] truncate rounded-md px-2 text-xs font-semibold text-fg transition-colors hover:bg-white/[0.06]"
    >
      {name}
    </button>
  )
}

function SaveIndicator() {
  const state = useEditor((s) => s.saveState)
  const lastSavedAt = useEditor((s) => s.lastSavedAt)
  const saveError = useEditor((s) => s.saveError)
  const flush = useEditor((s) => s.flushSave)
  const now = useNow(state === 'saved', 30000)
  if (state === 'error') {
    return (
      <button
        type="button"
        onClick={() => void flush()}
        data-tip={saveError ?? 'Save failed'}
        className="flex h-6 items-center gap-1 rounded-md bg-bad/10 px-2 text-2xs font-medium text-bad hover:bg-bad/20"
      >
        <CircleAlert className="h-3 w-3" /> Not saved · retry
      </button>
    )
  }
  return (
    <span
      className="flex h-6 items-center gap-1 px-1 text-2xs text-fg-4"
      data-tip={lastSavedAt ? `Saved ${relativeTime(new Date(lastSavedAt).toISOString(), now)}` : undefined}
      data-tip-kbd="Mod+S"
    >
      {state === 'saving' ? (
        <>
          <LoaderCircle className="h-3 w-3 animate-spin" /> Saving…
        </>
      ) : state === 'dirty' ? (
        <>
          <span className="h-1.5 w-1.5 rounded-full bg-warn/80" /> Edited
        </>
      ) : (
        <>
          <Check className="h-3 w-3 text-ok/80" /> Saved
        </>
      )}
    </span>
  )
}

function UndoRedo() {
  const canUndo = useEditor((s) => s.past.length > 0 && !s.busy)
  const canRedo = useEditor((s) => s.future.length > 0 && !s.busy)
  const undo = useEditor((s) => s.undo)
  const redo = useEditor((s) => s.redo)
  return (
    <div className="flex items-center">
      <IconButton label="Undo" kbd="Mod+Z" disabled={!canUndo} onClick={undo}>
        <Undo2 />
      </IconButton>
      <IconButton label="Redo" kbd="Mod+Shift+Z" disabled={!canRedo} onClick={redo}>
        <Redo2 />
      </IconButton>
    </div>
  )
}

function AppearanceSwitcher() {
  const appearance = useEditor((s) => s.project?.appearance ?? 'light')
  const platform = useEditor((s) => s.project?.canvas.platform ?? 'ios')
  const setAppearance = useEditor((s) => s.setAppearance)
  const presets = useAppStore((s) => s.presets.data)
  const allowed = presets?.platforms[platform]?.appearances ?? APPEARANCE_IDS
  return (
    <div className="flex items-center rounded-[9px] border border-line bg-surface-0 p-[3px]" role="radiogroup" aria-label="Appearance">
      {APPEARANCE_IDS.map((a, i) => {
        const active = a === appearance
        const supported = allowed.includes(a)
        const label = presets?.appearances[a]?.short ?? a
        return (
          <button
            key={a}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setAppearance(a as AppearanceId)}
            data-tip={`${presets?.appearances[a]?.label ?? a}${supported ? '' : ' — not used on this platform'}`}
            aria-label={presets?.appearances[a]?.label ?? a}
            data-tip-kbd={String(i + 1)}
            className={cn(
              'relative flex h-[26px] items-center gap-1.5 rounded-[6px] px-2 text-2xs font-medium transition-[background-color,color,box-shadow] duration-150',
              active ? 'bg-surface-4 text-fg shadow-[0_1px_0_rgb(255_255_255/0.07)_inset,0_1px_3px_rgb(0_0_0/0.4)]' : 'text-fg-3 hover:text-fg-2',
              !supported && !active && 'opacity-45',
            )}
          >
            <span
              className="h-3.5 w-3.5 shrink-0 rounded-[4px] border border-white/15"
              style={{ background: APPEARANCE_VISUAL[a].bg }}
            >
              <span className="flex h-full w-full items-center justify-center" style={{ color: APPEARANCE_VISUAL[a].fg }}>
                <AppearanceIcon appearance={a} className="h-2.5 w-2.5" />
              </span>
            </span>
            <span className="hidden 2xl:inline">{label}</span>
          </button>
        )
      })}
    </div>
  )
}

function PlatformPicker() {
  const platform = useEditor((s) => s.project?.canvas.platform ?? 'ios')
  const presets = useAppStore((s) => s.presets.data)
  const ref = useRef<HTMLButtonElement>(null)
  const menu = useAnchor<HTMLButtonElement>()
  const label = (id: string) => presets?.platforms[id]?.label ?? (id === 'free' ? 'Free-form' : id)
  return (
    <>
      <button
        ref={ref}
        type="button"
        onClick={() => ref.current && menu.toggle(ref.current)}
        data-tip={`Platform · ${label(platform)}`}
        aria-label={`Platform: ${label(platform)}`}
        aria-haspopup="menu"
        className={cn(
          'flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 text-2xs font-medium text-fg-2 transition-colors hover:bg-white/[0.06] hover:text-fg',
          menu.open && 'bg-white/[0.07] text-fg',
        )}
      >
        <PlatformIcon platform={platform} className="h-3.5 w-3.5" />
        <span className="hidden 2xl:inline">{label(platform)}</span>
        <ChevronDown className="h-3 w-3 text-fg-4" />
      </button>
      <Menu
        open={menu.open}
        onClose={menu.close}
        anchor={menu.anchor}
        placement="bottom-end"
        width={250}
        items={[
          { type: 'label', label: 'Target platform' },
          ...PLATFORM_IDS.map<MenuItem>((id) => {
            const spec = presets?.platforms[id]
            return {
              key: id,
              label: label(id),
              icon: <PlatformIcon platform={id} className="h-3.5 w-3.5" />,
              description: spec ? `${spec.canvas} px · ${spec.shape} · ${spec.appearances.length} appearance${spec.appearances.length === 1 ? '' : 's'}` : 'Any shape, no platform rules',
              checked: id === platform,
              onSelect: () => setPlatform(id),
            }
          }),
        ]}
      />
    </>
  )
}

function ViewToggle() {
  const view = useUi((s) => s.view3d)
  const set = useUi((s) => s.set)
  return (
    <Segmented
      size="sm"
      value={view}
      onChange={(v) => set({ view3d: v })}
      options={[
        { value: 'front', icon: <ScanLine />, tip: 'Front view (matches the Blender camera)', kbd: 'O' },
        { value: 'orbit', icon: <Orbit />, tip: 'Orbit view — drag to rotate the stack', kbd: 'O' },
      ]}
    />
  )
}

const SIZES = [null, 256, 512, 1024, 2048, 4096] as const

function RenderButton() {
  const presets = useAppStore((s) => s.presets.data)
  const render = useRender((s) => s.render)
  const menu = useAnchor<HTMLButtonElement>()
  const caretRef = useRef<HTMLButtonElement>(null)
  const [size, setSize] = useState<number | null>(null)
  const [fullBleed, setFullBleed] = useState(false)
  const lastSeconds = useRender((s) => (s.lastDone ? s.entries[s.lastDone]?.seconds : undefined))

  const go = (q: Quality) => void render(q, { size: size ?? undefined, fullBleed })
  const tiers: Quality[] = ['draft', 'preview', 'final', 'ultra']
  const items: MenuItem[] = tiers.map((q) => {
    const spec = presets?.quality[q]
    return {
      key: q,
      label: (
        <span className="flex items-center gap-2">
          {spec?.label ?? q}
          <span className="text-3xs font-normal text-fg-4">
            {spec?.engine === 'eevee' ? 'EEVEE' : 'Cycles'} · {size ?? spec?.size} px{spec?.engine === 'cycles' ? ` · ${spec.samples} spp` : ''}
          </span>
        </span>
      ),
      description: spec?.description,
      shortcut: q === 'preview' ? 'R' : q === 'final' ? 'Shift+R' : undefined,
      icon: <Play />,
      onSelect: () => go(q),
    }
  })

  return (
    <div className="flex items-center">
      <Button
        variant="secondary"
        size="sm"
        icon={<Play className="fill-current" />}
        className="rounded-r-none border-r-0"
        onClick={() => go('preview')}
        tipLabel={`Render Cycles preview${lastSeconds ? ` · last ${formatSeconds(lastSeconds)}` : ''}`}
        tipKbd="R"
      >
        Render
      </Button>
      <Button
        ref={caretRef}
        variant="secondary"
        size="sm"
        className="rounded-l-none px-1.5"
        onClick={() => caretRef.current && menu.toggle(caretRef.current)}
        aria-label="Render options"
      >
        <ChevronDown />
      </Button>
      <Menu
        open={menu.open}
        onClose={menu.close}
        anchor={menu.anchor}
        placement="bottom-end"
        width={300}
        items={items}
        header={
          <div className="space-y-2 border-b border-line px-2 pb-2.5 pt-1.5">
            <div className="flex items-center justify-between">
              <span className="text-3xs font-semibold uppercase tracking-[0.08em] text-fg-4">Size</span>
              <div className="flex gap-0.5">
                {SIZES.map((s) => (
                  <button
                    key={String(s)}
                    type="button"
                    onClick={() => setSize(s)}
                    className={cn(
                      'h-5 rounded px-1.5 text-3xs font-medium tabular transition-colors',
                      size === s ? 'bg-accent/25 text-fg' : 'text-fg-3 hover:bg-white/[0.07]',
                    )}
                  >
                    {s ?? 'Tier'}
                  </button>
                ))}
              </div>
            </div>
            <label className="flex cursor-pointer items-center justify-between text-2xs text-fg-3">
              <span>Full-bleed square (App Store master)</span>
              <input type="checkbox" checked={fullBleed} onChange={(e) => setFullBleed(e.target.checked)} className="accent-[var(--color-accent)]" />
            </label>
            {size != null && size > 1024 && (
              <div className="rounded-md bg-warn/10 px-2 py-1 text-3xs text-warn">Large renders use a one-shot Blender process and take a while.</div>
            )}
          </div>
        }
      />
    </div>
  )
}

function PanelToggles() {
  const leftCollapsed = useUi((s) => s.leftCollapsed)
  const rightCollapsed = useUi((s) => s.rightCollapsed)
  const set = useUi((s) => s.set)
  return (
    <div className="flex items-center">
      <IconButton label={leftCollapsed ? 'Show layers' : 'Hide layers'} kbd="Mod+\" active={!leftCollapsed} onClick={() => set({ leftCollapsed: !leftCollapsed })}>
        <PanelLeft />
      </IconButton>
      <IconButton label={rightCollapsed ? 'Show inspector' : 'Hide inspector'} kbd="Mod+\" active={!rightCollapsed} onClick={() => set({ rightCollapsed: !rightCollapsed })}>
        <PanelRight />
      </IconButton>
    </div>
  )
}
