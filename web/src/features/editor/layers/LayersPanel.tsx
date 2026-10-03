import { memo, useCallback, useMemo, useRef, useState } from 'react'
import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type Modifier,
} from '@dnd-kit/core'
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  ArrowDownToLine,
  ArrowUpToLine,
  ChevronRight,
  Combine,
  Eye,
  EyeOff,
  Image as ImageIcon,
  Layers,
  Lock,
  LockOpen,
  MoveRight,
  PenLine,
  Plus,
  Spline,
  Split,
  SquareDashedMousePointer,
  Trash2,
  WandSparkles,
} from 'lucide-react'
import type { GeometryBundle, Layer, Presets, Project, SvgElement } from '../../../types'
import { useEvent } from '../../../lib/hooks'
import { projectsApi } from '../../../api'
import { cn, hashString } from '../../../lib/format'
import { effectivePlateFill } from '../../../lib/projectOps'
import { fillToCss } from '../../../lib/color'
import { MATERIAL_CSS, STRATEGIES } from '../../../lib/meta'
import { useAppStore } from '../../../store/app'
import { useEditor, type SelectMode } from '../../../store/editor'
import { useUi } from '../../../store/ui'
import { ShapeIcon } from '../../../components/icons'
import { Badge, EmptyState, IconButton, Menu, useAnchor, type MenuItem } from '../../../components/ui'
import { moveSelectedLayer } from '../shortcuts'

const verticalOnly: Modifier = ({ transform }) => ({ ...transform, x: 0 })

function modeFrom(e: React.MouseEvent): SelectMode {
  return e.shiftKey ? 'range' : e.ctrlKey || e.metaKey ? 'toggle' : 'replace'
}

export function LayersPanel() {
  const project = useEditor((s) => s.project)!
  const geometry = useEditor((s) => s.geometry)
  const selection = useEditor((s) => s.selection)
  const busy = useEditor((s) => s.busy)
  const presets = useAppStore((s) => s.presets.data)!
  const ed = useEditor.getState
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [renaming, setRenaming] = useState<string | null>(null)
  const ctx = useAnchor()
  const [ctxTarget, setCtxTarget] = useState<{ kind: 'layer' | 'element'; id: string } | null>(null)
  const resplitMenu = useAnchor<HTMLButtonElement>()
  const splitMenu = useAnchor<HTMLButtonElement>()
  const moveMenu = useAnchor<HTMLButtonElement>()
  const resplitRef = useRef<HTMLButtonElement>(null)
  const splitRef = useRef<HTMLButtonElement>(null)
  const moveRef = useRef<HTMLButtonElement>(null)

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const display = useMemo(() => [...project.layers].reverse(), [project.layers])
  // Stable sortable ids (a new array per render would change dnd-kit's context and re-render every row).
  const orderKey = display.map((l) => l.id).join('|')
  const sortableIds = useMemo(() => (orderKey ? orderKey.split('|') : []), [orderKey])
  const elements = useMemo(() => new Map(project.elements.map((e) => [e.id, e])), [project.elements])
  const sel = new Set(selection.layerIds)
  const primary = selection.primary ? project.layers.find((l) => l.id === selection.primary) : undefined

  const onDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = project.layers.findIndex((l) => l.id === active.id)
    const to = project.layers.findIndex((l) => l.id === over.id)
    if (from >= 0 && to >= 0) ed().reorder(from, to)
  }

  const layerMenu = (layer: Layer): MenuItem[] => {
    const multi = selection.layerIds.length > 1 && sel.has(layer.id)
    const idx = project.layers.findIndex((l) => l.id === layer.id)
    return [
      { label: 'Rename', icon: <PenLine />, onSelect: () => setRenaming(layer.id) },
      {
        label: layer.visible ? 'Hide' : 'Show',
        icon: layer.visible ? <EyeOff /> : <Eye />,
        shortcut: 'H',
        onSelect: () => ed().commit((p) => ({ ...p, layers: p.layers.map((l) => (l.id === layer.id ? { ...l, visible: !l.visible } : l)) })),
      },
      {
        label: layer.locked ? 'Unlock' : 'Lock',
        icon: layer.locked ? <LockOpen /> : <Lock />,
        onSelect: () => ed().commit((p) => ({ ...p, layers: p.layers.map((l) => (l.id === layer.id ? { ...l, locked: !l.locked } : l)) }), { render: false }),
      },
      { type: 'separator' },
      { label: multi ? `Merge ${selection.layerIds.length} layers` : 'Merge with layer below', icon: <Combine />, shortcut: 'Mod+G', disabled: !multi && idx <= 0, onSelect: () => void ed().mergeLayers(multi ? selection.layerIds : [project.layers[idx - 1].id, layer.id]) },
      { label: 'Split into elements', icon: <Split />, disabled: layer.elementIds.length < 2, onSelect: () => void ed().splitLayer(layer.id, 'elements') },
      { label: 'Split into islands', icon: <Spline />, description: 'Separate disconnected shapes.', onSelect: () => void ed().splitLayer(layer.id, 'islands') },
      { type: 'separator' },
      { label: 'Bring forward', icon: <ArrowUpToLine />, shortcut: 'Mod+]', disabled: idx >= project.layers.length - 1, onSelect: () => { ed().selectLayer(layer.id); moveSelectedLayer(1) } },
      { label: 'Send backward', icon: <ArrowDownToLine />, shortcut: 'Mod+[', disabled: idx <= 0, onSelect: () => { ed().selectLayer(layer.id); moveSelectedLayer(-1) } },
      { type: 'separator' },
      { label: multi ? `Delete ${selection.layerIds.length} layers` : 'Delete layer', icon: <Trash2 />, shortcut: 'Del', danger: true, onSelect: () => ed().deleteLayers(multi ? selection.layerIds : [layer.id]) },
    ]
  }

  const moveItems = (elementIds: string[]): MenuItem[] => {
    const from = new Set(elementIds.map((id) => project.layers.find((l) => l.elementIds.includes(id))?.id))
    return [
      { type: 'label', label: `Move ${elementIds.length} element${elementIds.length === 1 ? '' : 's'} to` },
      ...display
        .filter((l) => !(from.size === 1 && from.has(l.id)))
        .map<MenuItem>((l) => ({ key: l.id, label: l.name, icon: <Layers />, onSelect: () => void ed().moveElements(elementIds, l.id) })),
      { type: 'separator' },
      { label: 'New layer from selection', icon: <Plus />, onSelect: () => void ed().moveElements(elementIds, null) },
    ]
  }

  const ctxItems: MenuItem[] = (() => {
    if (!ctxTarget) return []
    if (ctxTarget.kind === 'layer') {
      const l = project.layers.find((x) => x.id === ctxTarget.id)
      return l ? layerMenu(l) : []
    }
    const ids = selection.elementIds.includes(ctxTarget.id) ? selection.elementIds : [ctxTarget.id]
    return moveItems(ids)
  })()

  // Stable row callbacks: LayerRow is memoised, so a slider drag on one layer re-renders only that row.
  const onToggleExpand = useCallback((id: string) => setExpanded((x) => ({ ...x, [id]: !x[id] })), [])
  const onRenameStart = useCallback((id: string) => setRenaming(id), [])
  const onRenameDone = useCallback(() => setRenaming(null), [])
  const onContext = useEvent((e: React.MouseEvent, kind: 'layer' | 'element', id: string) => {
    e.preventDefault()
    const cur = ed().selection
    if (kind === 'layer' && !cur.layerIds.includes(id)) ed().selectLayer(id)
    if (kind === 'element' && !cur.elementIds.includes(id)) ed().selectElement(id)
    setCtxTarget({ kind, id })
    ctx.openAt({ x: e.clientX, y: e.clientY })
  })

  const overLimit = project.layers.length > 4

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* header */}
      <div className="flex h-9 shrink-0 items-center gap-1.5 border-b border-line pl-3 pr-1.5">
        <Layers className="h-3.5 w-3.5 text-fg-3" />
        <span className="text-2xs font-semibold text-fg-2">Layers</span>
        <Badge
          tone={overLimit ? 'warn' : 'neutral'}
          tip={
            overLimit
              ? `${project.layers.length} groups — Apple Icon Composer allows at most 4. The .icon export will flatten extra groups; renders are unaffected.`
              : `${project.layers.length} of 4 Icon Composer groups`
          }
        >
          {project.layers.length}
          {overLimit ? ' / 4 max' : ''}
        </Badge>
        <div className="flex-1" />
        <IconButton
          ref={resplitRef}
          label="Re-split artwork…"
          disabled={!!busy}
          onClick={() => resplitRef.current && resplitMenu.toggle(resplitRef.current)}
          active={resplitMenu.open}
        >
          <WandSparkles />
        </IconButton>
      </div>

      {/* list */}
      <div
        className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden py-1"
        onClick={(e) => {
          if (e.target === e.currentTarget) ed().clearSelection()
        }}
      >
        {display.length === 0 ? (
          <EmptyState icon={<Layers />} title="No layers">
            Re-split the artwork to rebuild the layer stack.
          </EmptyState>
        ) : (
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd} modifiers={[verticalOnly]}>
            <SortableContext items={sortableIds} strategy={verticalListSortingStrategy}>
              {display.map((layer) => (
                <LayerRow
                  key={layer.id}
                  layer={layer}
                  projectId={project.id}
                  presets={presets}
                  geometry={geometry}
                  elements={elements}
                  selected={sel.has(layer.id)}
                  primary={selection.primary === layer.id}
                  selectedElements={selection.elementIds}
                  expanded={!!expanded[layer.id]}
                  renaming={renaming === layer.id}
                  onToggleExpand={onToggleExpand}
                  onRenameDone={onRenameDone}
                  onRenameStart={onRenameStart}
                  onContext={onContext}
                />
              ))}
            </SortableContext>
          </DndContext>
        )}
        <CanvasRow project={project} />
      </div>

      {/* footer */}
      <div className="flex h-9 shrink-0 items-center gap-0.5 border-t border-line px-1.5">
        <IconButton label="Merge selected layers" kbd="Mod+G" disabled={selection.layerIds.length < 2 || !!busy} onClick={() => void ed().mergeLayers()}>
          <Combine />
        </IconButton>
        <IconButton
          ref={splitRef}
          label="Split layer…"
          disabled={!primary || !!busy}
          onClick={() => splitRef.current && splitMenu.toggle(splitRef.current)}
        >
          <Split />
        </IconButton>
        <IconButton
          ref={moveRef}
          label={selection.elementIds.length ? `Move ${selection.elementIds.length} element(s) to layer…` : 'Select elements (expand a layer) to move them'}
          disabled={!selection.elementIds.length || !!busy}
          onClick={() => moveRef.current && moveMenu.toggle(moveRef.current)}
        >
          <MoveRight />
        </IconButton>
        <div className="flex-1" />
        <span className="truncate px-1 text-3xs text-fg-4">
          {selection.elementIds.length
            ? `${selection.elementIds.length} element${selection.elementIds.length > 1 ? 's' : ''}`
            : selection.layerIds.length > 1
              ? `${selection.layerIds.length} selected`
              : ''}
        </span>
        <IconButton label="Delete selected layers" kbd="Del" disabled={!selection.layerIds.length || !!busy} onClick={() => ed().deleteLayers()}>
          <Trash2 />
        </IconButton>
      </div>

      <Menu open={ctx.open} onClose={ctx.close} anchor={ctx.anchor} items={ctxItems} width={240} />
      <Menu
        open={resplitMenu.open}
        onClose={resplitMenu.close}
        anchor={resplitMenu.anchor}
        placement="bottom-end"
        width={290}
        items={[
          { type: 'label', label: 'Re-split the artwork' },
          ...STRATEGIES.map<MenuItem>((s) => ({
            key: s.id,
            label: s.label,
            description: s.description,
            checked: project.strategy === s.id,
            icon: <WandSparkles />,
            onSelect: () => void ed().resplit(s.id),
          })),
          { type: 'separator' },
          { type: 'label', label: 'Replaces the layer stack (undoable).' },
        ]}
      />
      <Menu
        open={splitMenu.open}
        onClose={splitMenu.close}
        anchor={splitMenu.anchor}
        placement="top-start"
        width={240}
        items={
          primary
            ? [
                { label: 'Split into elements', icon: <Split />, description: 'One layer per SVG element.', disabled: primary.elementIds.length < 2, onSelect: () => void ed().splitLayer(primary.id, 'elements') },
                { label: 'Split into islands', icon: <Spline />, description: 'One layer per disconnected shape.', onSelect: () => void ed().splitLayer(primary.id, 'islands') },
              ]
            : []
        }
      />
      <Menu open={moveMenu.open} onClose={moveMenu.close} anchor={moveMenu.anchor} placement="top-start" width={240} items={moveItems(selection.elementIds)} />
    </div>
  )
}

// ------------------------------------------------------------------------------------------ rows
interface LayerRowProps {
  layer: Layer
  projectId: string
  presets: Presets
  geometry: GeometryBundle | null
  elements: Map<string, SvgElement>
  selected: boolean
  primary: boolean
  selectedElements: string[]
  expanded: boolean
  renaming: boolean
  onToggleExpand: (layerId: string) => void
  onRenameStart: (layerId: string) => void
  onRenameDone: () => void
  onContext: (e: React.MouseEvent, kind: 'layer' | 'element', id: string) => void
}

const LayerRow = memo(function LayerRow({
  layer,
  projectId,
  presets,
  geometry,
  elements,
  selected,
  primary,
  selectedElements,
  expanded,
  renaming,
  onToggleExpand,
  onRenameStart,
  onRenameDone,
  onContext,
}: LayerRowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: layer.id })
  const ed = useEditor.getState
  const version = geometry?.layers[layer.id]?.hash ?? hashString(layer.elementIds.join(','))
  const thumb = projectsApi.layerThumbnailUrl(projectId, layer.id, version)
  const material = presets.materials[layer.material.preset]
  const geo = geometry?.layers[layer.id]
  const clamped = geo && layer.depth.bevel > 0.9 * geo.safeRadius

  const toggle = (field: 'visible' | 'locked') =>
    ed().commit((p) => ({ ...p, layers: p.layers.map((l) => (l.id === layer.id ? { ...l, [field]: !l[field] } : l)) }), { render: field === 'visible' })

  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={cn('relative', isDragging && 'z-10')}
    >
      <div
        {...attributes}
        {...listeners}
        role="treeitem"
        aria-selected={selected}
        onClick={(e) => {
          e.stopPropagation()
          ed().selectLayer(layer.id, modeFrom(e))
          useUi.getState().set({ inspectorTab: 'layer' })
        }}
        onDoubleClick={(e) => {
          if ((e.target as HTMLElement).closest('[data-name]')) onRenameStart(layer.id)
        }}
        onContextMenu={(e) => onContext(e, 'layer', layer.id)}
        className={cn(
          'group relative mx-1 flex h-10 items-center gap-1.5 rounded-md pl-1 pr-1 outline-none transition-colors',
          isDragging ? 'bg-surface-4 shadow-pop' : selected ? (primary ? 'bg-accent/[0.16]' : 'bg-accent/[0.09]') : 'hover:bg-white/[0.04]',
          !layer.visible && 'opacity-55',
        )}
      >
        {selected && <span className="absolute inset-y-1.5 left-0 w-[2px] rounded-full accent-gradient" />}
        <button
          type="button"
          aria-label={expanded ? 'Collapse' : 'Expand'}
          onClick={(e) => {
            e.stopPropagation()
            onToggleExpand(layer.id)
          }}
          onPointerDown={(e) => e.stopPropagation()}
          className="flex h-5 w-4 shrink-0 items-center justify-center text-fg-4 hover:text-fg-2"
        >
          <ChevronRight className={cn('h-3 w-3 transition-transform duration-150', expanded && 'rotate-90')} />
        </button>
        <div className="relative h-7 w-7 shrink-0 overflow-hidden rounded-[6px] border border-line-2 checkerboard-sm">
          {/* A thumbnail can 400 for a moment while an undo/redo is still being saved: hide it, and show it
              again once a later version loads (the <img> element is reused across src changes). */}
          <img
            src={thumb}
            alt=""
            draggable={false}
            loading="lazy"
            className="absolute inset-0 h-full w-full object-contain"
            onError={(e) => (e.currentTarget.style.opacity = '0')}
            onLoad={(e) => (e.currentTarget.style.opacity = '')}
          />
        </div>
        <div className="min-w-0 flex-1" data-name="">
          {renaming ? (
            <RenameInput
              initial={layer.name}
              onDone={(name) => {
                if (name && name !== layer.name) ed().commit((p) => ({ ...p, layers: p.layers.map((l) => (l.id === layer.id ? { ...l, name } : l)) }), { render: false })
                onRenameDone()
              }}
            />
          ) : (
            <div className="truncate text-xs font-medium text-fg" title="Double-click to rename">
              {layer.name}
            </div>
          )}
          <div className="flex items-center gap-1 truncate text-3xs text-fg-4">
            <span
              className="inline-block h-2 w-2 shrink-0 rounded-full border border-white/20"
              style={{ background: material?.swatch ? `center/cover url(${material.swatch})` : (MATERIAL_CSS[layer.material.preset] ?? '#666') }}
            />
            <span className="truncate">{material?.label ?? layer.material.preset}</span>
            <span>·</span>
            <span className="shrink-0">{layer.elementIds.length} el</span>
            {!layer.glass && <span className="shrink-0 text-fg-3">· flat</span>}
            {clamped && <span className="shrink-0 text-warn" data-tip="Bevel clamped by thin features">· bevel↓</span>}
          </div>
        </div>
        <div className={cn('flex shrink-0 items-center', !layer.locked && layer.visible && 'opacity-0 transition-opacity group-hover:opacity-100', selected && 'opacity-100')}>
          <button
            type="button"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation()
              toggle('locked')
            }}
            data-tip={layer.locked ? 'Unlock' : 'Lock'}
            aria-label={layer.locked ? `Unlock ${layer.name}` : `Lock ${layer.name}`}
            className={cn('flex h-6 w-6 items-center justify-center rounded text-fg-4 hover:bg-white/[0.08] hover:text-fg', layer.locked && 'text-warn/90')}
          >
            {layer.locked ? <Lock className="h-3 w-3" /> : <LockOpen className="h-3 w-3" />}
          </button>
          <button
            type="button"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation()
              toggle('visible')
            }}
            data-tip={layer.visible ? 'Hide' : 'Show'}
            aria-label={layer.visible ? `Hide ${layer.name}` : `Show ${layer.name}`}
            data-tip-kbd="H"
            className="flex h-6 w-6 items-center justify-center rounded text-fg-3 hover:bg-white/[0.08] hover:text-fg"
          >
            {layer.visible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>
      {expanded && (
        <div className="relative mb-1 ml-[22px] mr-1 border-l border-line-2 pl-1 animate-fade-in">
          {[...layer.elementIds].reverse().map((id) => {
            const el = elements.get(id)
            if (!el) return null
            return (
              <ElementRow
                key={id}
                element={el}
                selected={selectedElements.includes(id)}
                onSelect={(e) => {
                  e.stopPropagation()
                  ed().selectElement(id, modeFrom(e))
                }}
                onContext={(e) => onContext(e, 'element', id)}
              />
            )
          })}
        </div>
      )}
    </div>
  )
})

function ElementRow({
  element,
  selected,
  onSelect,
  onContext,
}: {
  element: SvgElement
  selected: boolean
  onSelect: (e: React.MouseEvent) => void
  onContext: (e: React.MouseEvent) => void
}) {
  const chip = fillToCss(element.paint, '#555')
  return (
    <div
      role="treeitem"
      aria-selected={selected}
      onClick={onSelect}
      onContextMenu={onContext}
      className={cn(
        'flex h-6 items-center gap-1.5 rounded px-1.5 text-2xs transition-colors',
        selected ? 'bg-accent-2/[0.12] text-fg' : 'text-fg-3 hover:bg-white/[0.04] hover:text-fg-2',
      )}
    >
      <span className="relative h-3 w-3 shrink-0 overflow-hidden rounded-[3px] border border-white/20 checkerboard-sm">
        <span className="absolute inset-0" style={{ background: chip, opacity: element.opacity }} />
      </span>
      <span className="min-w-0 flex-1 truncate">{element.name || element.id}</span>
      {element.kind === 'image' && <ImageIcon className="h-3 w-3 shrink-0 text-fg-4" />}
      {element.role === 'stroke' && <span className="shrink-0 text-3xs text-fg-4">stroke</span>}
      {element.shadow && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-fg-4" data-tip="Has a drop shadow in the source" />}
      <span className="w-7 shrink-0 text-right text-3xs tabular text-fg-4">{(element.area * 100).toFixed(element.area < 0.01 ? 1 : 0)}%</span>
    </div>
  )
}

function RenameInput({ initial, onDone }: { initial: string; onDone: (name: string) => void }) {
  const [v, setV] = useState(initial)
  const done = useRef(false)
  const finish = (name: string) => {
    if (done.current) return
    done.current = true
    onDone(name.trim())
  }
  return (
    <input
      autoFocus
      value={v}
      onChange={(e) => setV(e.target.value)}
      onFocus={(e) => e.target.select()}
      onPointerDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation()
        if (e.key === 'Enter') finish(v)
        if (e.key === 'Escape') finish(initial)
      }}
      onBlur={() => finish(v)}
      className="h-5 w-full rounded border border-accent/60 bg-surface-0 px-1 text-xs text-fg outline-none"
    />
  )
}

function CanvasRow({ project }: { project: Project }) {
  const selection = useEditor((s) => s.selection)
  const tab = useUi((s) => s.inspectorTab)
  const active = tab === 'document' && selection.layerIds.length === 0
  const plateFill = effectivePlateFill(project, project.appearance)
  return (
    <div className="mx-1 mt-1 border-t border-line pt-1">
      <div
        role="treeitem"
        aria-selected={active}
        onClick={() => {
          useEditor.getState().clearSelection()
          useUi.getState().set({ inspectorTab: 'document' })
        }}
        className={cn(
          'relative flex h-10 cursor-default items-center gap-2 rounded-md pl-[22px] pr-2 transition-colors',
          active ? 'bg-accent/[0.12]' : 'hover:bg-white/[0.04]',
        )}
      >
        {active && <span className="absolute inset-y-1.5 left-0 w-[2px] rounded-full accent-gradient" />}
        <div className="relative h-7 w-7 shrink-0 overflow-hidden rounded-[7px] border border-line-2 checkerboard-sm">
          {project.canvas.plate.visible && project.canvas.shape !== 'none' && (
            <span className="absolute inset-[3px] rounded-[5px]" style={{ background: fillToCss(plateFill, '#888') }} />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium text-fg-2">Canvas & plate</div>
          <div className="flex items-center gap-1 text-3xs text-fg-4">
            <ShapeIcon shape={project.canvas.shape} className="h-2.5 w-2.5" />
            <span className="capitalize">{project.canvas.shape}</span>
            <span>·</span>
            <span className="uppercase">{project.canvas.platform}</span>
          </div>
        </div>
        <SquareDashedMousePointer className="h-3.5 w-3.5 text-fg-4" />
      </div>
    </div>
  )
}
