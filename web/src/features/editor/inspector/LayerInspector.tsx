import { useMemo } from 'react'
import {
  AlignVerticalSpaceAround,
  Blend,
  Box,
  Eye,
  EyeOff,
  Gem,
  Layers,
  Lock,
  LockOpen,
  Move,
  MousePointerClick,
  Palette,
  RotateCcw,
  SunDim,
  TriangleAlert,
} from 'lucide-react'
import type { BlendMode, Fill, Layer, LayerOverride, MaterialSpec } from '../../../types'
import { projectsApi } from '../../../api'
import { cn, formatNumber, hashString } from '../../../lib/format'
import { effectiveLayer, setLayerOverride, updateLayers, withParam, type OverrideField } from '../../../lib/projectOps'
import { paintColor } from '../../../lib/color'
import { isReservedParam } from '../../../lib/looks'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { useUi } from '../../../store/ui'
import { Button, EmptyState, IconButton, Row, Section, Segmented, Select, SliderRow, Switch, type ChangePhase } from '../../../components/ui'
import { FillEditor } from './FillEditor'
import { MaterialGallery, MaterialParams } from './MaterialGallery'
import { OverrideRows, ScopePicker, useScope } from './scope'

const BLEND_MODES: { value: BlendMode; label: string }[] = [
  { value: 'normal', label: 'Normal' },
  { value: 'darken', label: 'Darken' },
  { value: 'multiply', label: 'Multiply' },
  { value: 'plus-darker', label: 'Plus Darker' },
  { value: 'lighten', label: 'Lighten' },
  { value: 'screen', label: 'Screen' },
  { value: 'plus-lighter', label: 'Plus Lighter' },
  { value: 'overlay', label: 'Overlay' },
  { value: 'soft-light', label: 'Soft Light' },
  { value: 'hard-light', label: 'Hard Light' },
]

export function LayerInspector() {
  const project = useEditor((s) => s.project)!
  const selection = useEditor((s) => s.selection)
  const geometry = useEditor((s) => s.geometry)
  const commit = useEditor((s) => s.commit)
  const presets = useAppStore((s) => s.presets.data)!
  const colorScope = useScope('color')
  const materialScope = useScope('material')

  const layers = useMemo(
    () => selection.layerIds.map((id) => project.layers.find((l) => l.id === id)).filter((l): l is Layer => !!l),
    [selection.layerIds, project.layers],
  )

  if (!layers.length) {
    return (
      <EmptyState
        icon={<MousePointerClick />}
        title="No layer selected"
        className="py-14"
        action={
          <Button size="sm" variant="secondary" onClick={() => useUi.getState().set({ inspectorTab: 'document' })}>
            Edit canvas & lighting
          </Button>
        }
      >
        Click a layer in the stack or in the viewport. Ctrl/Shift-click to edit several layers at once.
      </EmptyState>
    )
  }

  const primary = layers.find((l) => l.id === selection.primary) ?? layers[layers.length - 1]
  const editable = layers.filter((l) => !l.locked)
  const ids = editable.map((l) => l.id)
  const allLocked = ids.length === 0
  const key = ids.join(',')

  const editBase = (field: string, fn: (l: Layer) => Layer) => commit((p) => updateLayers(p, ids, fn), { coalesce: `${key}:${field}` })

  /** Write a per-appearance-overridable field according to the section's scope. */
  const editField = <K extends OverrideField>(scope: { bucket: 'dark' | 'mono' | null }, field: K, value: NonNullable<LayerOverride[K]>) => {
    if (scope.bucket) {
      const bucket = scope.bucket
      commit((p) => setLayerOverride(p, bucket, ids, { [field]: value } as Partial<LayerOverride>), { coalesce: `ov:${bucket}:${key}:${field}` })
    } else {
      editBase(field, (l) => (l[field as keyof Layer] === value ? l : { ...l, [field]: value }))
    }
  }

  /** A material *parameter* edit only touches that parameter, and only on selected layers that use the same
   *  preset — multi-selecting a glass and a chrome layer and dragging "Tint" must not turn the chrome into glass. */
  const editMaterialParam = (spec: MaterialSpec, paramKey: string) => {
    const apply = (m: MaterialSpec): MaterialSpec => {
      if (m.preset !== spec.preset) return m
      if (paramKey === '*') return Object.keys(m.params ?? {}).some((k) => !isReservedParam(k)) ? { ...m, params: {} } : m
      return m.params?.[paramKey] === spec.params[paramKey] ? m : withParam(m, paramKey, spec.params[paramKey])
    }
    const bucket = materialScope.bucket
    if (bucket) {
      commit(
        (p) => {
          let next = p
          for (const l of p.layers) {
            if (!ids.includes(l.id)) continue
            const cur = effectiveLayer(p, p.appearance, l).material
            const m = apply(cur)
            if (m !== cur) next = setLayerOverride(next, bucket, [l.id], { material: m })
          }
          return next
        },
        { coalesce: `ov:${bucket}:${key}:material.${paramKey}` },
      )
    } else {
      editBase(`material.${paramKey}`, (l) => {
        const m = apply(l.material)
        return m === l.material ? l : { ...l, material: m }
      })
    }
  }

  const colorView = colorScope.bucket ? effectiveLayer(project, project.appearance, primary) : primary
  const materialView = materialScope.bucket ? effectiveLayer(project, project.appearance, primary) : primary
  const material = materialView.material
  const preset = presets.materials[material.preset]
  const geo = geometry?.layers[primary.id]
  const maxBevel = geo ? 0.9 * geo.safeRadius : null
  const bevelClamped = maxBevel != null && primary.depth.bevel > maxBevel + 1e-6
  const firstElementColor = paintColor(project.elements.find((e) => primary.elementIds.includes(e.id))?.paint) ?? undefined
  const version = geo?.hash ?? hashString(primary.elementIds.join(','))

  return (
    <div className="pb-6">
      {/* header */}
      <div className="flex items-center gap-2.5 border-b border-line px-3 py-2.5">
        <div className="relative h-11 w-11 shrink-0 overflow-hidden rounded-lg border border-line-2 checkerboard-sm">
          <img
            src={projectsApi.layerThumbnailUrl(project.id, primary.id, version)}
            alt=""
            className="absolute inset-0 h-full w-full object-contain"
            onError={(e) => (e.currentTarget.style.opacity = '0')}
            onLoad={(e) => (e.currentTarget.style.opacity = '')}
          />
          {layers.length > 1 && (
            <span className="absolute bottom-0 right-0 rounded-tl-md bg-accent px-1 text-[9px] font-bold text-white">{layers.length}</span>
          )}
        </div>
        <div className="min-w-0 flex-1">
          {layers.length > 1 ? (
            <div className="h-6 truncate text-[13px] font-semibold leading-6 text-fg">{layers.length} layers</div>
          ) : (
            <input
              key={`${primary.id}:${primary.name}`}
              defaultValue={primary.name}
              aria-label="Layer name"
              onKeyDown={(e) => {
                e.stopPropagation()
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              }}
              onBlur={(e) => {
                const name = e.target.value.trim()
                if (name && name !== primary.name) commit((p) => updateLayers(p, [primary.id], (l) => ({ ...l, name })), { render: false })
              }}
              className="-ml-1 h-6 w-full rounded border border-transparent bg-transparent px-1 text-[13px] font-semibold text-fg outline-none transition-colors hover:border-line focus:border-accent/60 focus:bg-surface-0"
            />
          )}
          <div className="truncate text-3xs text-fg-4">
            {layers.length > 1
              ? 'Edits apply to every selected layer'
              : `${primary.elementIds.length} element${primary.elementIds.length === 1 ? '' : 's'} · z ${formatNumber(primary.depth.z, 2)} · ${primary.mode}`}
          </div>
        </div>
        <IconButton label={primary.visible ? 'Hide' : 'Show'} kbd="H" onClick={() => editBase('visible', (l) => ({ ...l, visible: !primary.visible }))} disabled={allLocked}>
          {primary.visible ? <Eye /> : <EyeOff />}
        </IconButton>
        <IconButton
          label={primary.locked ? 'Unlock' : 'Lock'}
          active={primary.locked}
          onClick={() => commit((p) => updateLayers(p, layers.map((l) => l.id), (l) => ({ ...l, locked: !primary.locked })), { render: false })}
        >
          {primary.locked ? <Lock /> : <LockOpen />}
        </IconButton>
      </div>

      {allLocked && (
        <div className="mx-3 mt-3 flex items-center gap-2 rounded-lg border border-warn/25 bg-warn/[0.07] px-2.5 py-2 text-2xs text-warn">
          <Lock className="h-3.5 w-3.5" /> Locked — unlock to edit.
        </div>
      )}

      <div className={cn(allLocked && 'pointer-events-none opacity-45')}>
        {/* Material */}
        <Section id="layer.material" title="Material" icon={<Gem />} right={<ScopePicker section="material" />}>
          <MaterialGallery
            value={material.preset}
            presets={presets}
            onChange={(id) => {
              if (id !== material.preset) editField(materialScope, 'material', { preset: id, params: {} } as MaterialSpec)
            }}
          />
          <MaterialParams preset={preset} spec={material} onChange={(spec, _phase, paramKey) => editMaterialParam(spec, paramKey)} />
          {!materialScope.bucket && <OverrideRows project={project} layerIds={[primary.id]} field="material" presets={presets} />}
          <div className="!mt-3 space-y-[7px] border-t border-line pt-3">
            <Row label="Glass effects" hint="Icon Composer 'Effects': off = flat inlay without glass highlights.">
              <Switch checked={primary.glass} onChange={(v) => editBase('glass', (l) => ({ ...l, glass: v }))} label="Glass effects" />
              <span className="text-3xs text-fg-4">{primary.glass ? 'Lit 3D body' : 'Flat inlay'}</span>
            </Row>
            <Row label="Mode" hint="Individual: every element is its own piece of glass. Combined: one body around the union (no inner rims).">
              <Segmented
                size="xs"
                fill
                value={primary.mode}
                onChange={(v) => editBase('mode', (l) => ({ ...l, mode: v }))}
                options={[
                  { value: 'individual', label: 'Individual', tip: 'Each element is its own piece of glass' },
                  { value: 'combined', label: 'Combined', tip: 'One glass body around the union' },
                ]}
              />
            </Row>
          </div>
        </Section>

        {/* Colour */}
        <Section id="layer.color" title="Colour" icon={<Palette />} right={<ScopePicker section="color" />}>
          <FillEditor
            fill={colorView.fill}
            fallbackColor={firstElementColor}
            onChange={(f: Fill, phase: ChangePhase) => {
              void phase
              editField(colorScope, 'fill', f)
            }}
          />
          {!colorScope.bucket && <OverrideRows project={project} layerIds={[primary.id]} field="fill" presets={presets} />}
          <SliderRow
            label="Opacity"
            value={colorView.opacity}
            min={0}
            max={1}
            step={0.01}
            scale={100}
            decimals={0}
            unit="%"
            defaultValue={1}
            onChange={(v) => editField(colorScope, 'opacity', v)}
          />
          {!colorScope.bucket && <OverrideRows project={project} layerIds={[primary.id]} field="opacity" presets={presets} />}
          <Row label="Blend mode">
            <Select
              value={colorView.blendMode}
              options={BLEND_MODES.map((b) => ({ value: b.value, label: b.label, icon: <Blend /> }))}
              onChange={(v) => editField(colorScope, 'blendMode', v)}
              className="flex-1"
            />
          </Row>
          {!colorScope.bucket && <OverrideRows project={project} layerIds={[primary.id]} field="blendMode" presets={presets} />}
          {colorScope.bucket && (
            <Row label="Visible here">
              <Switch checked={colorView.visible} onChange={(v) => editField(colorScope, 'visible', v)} label="Visible in this appearance" />
              <span className="text-3xs text-fg-4">only for this appearance</span>
            </Row>
          )}
          {!colorScope.bucket && <OverrideRows project={project} layerIds={[primary.id]} field="visible" presets={presets} />}
        </Section>

        {/* Depth */}
        <Section
          id="layer.depth"
          title="Depth & bevel"
          icon={<Box />}
          right={
            <IconButton
              label="Re-stack all layers evenly (z = i × 0.13)"
              size="xs"
              onClick={() => commit((p) => ({ ...p, layers: p.layers.map((l, i) => (l.locked ? l : { ...l, depth: { ...l.depth, z: Math.round(i * 0.13 * 1000) / 1000 } })) }), { coalesce: 'restack' })}
            >
              <AlignVerticalSpaceAround />
            </IconButton>
          }
        >
          <SliderRow label="Z offset" hint="Back face above the plate front (art units)." value={primary.depth.z} min={0} max={1.2} step={0.005} soft onChange={(v) => editBase('depth.z', (l) => ({ ...l, depth: { ...l.depth, z: v } }))} />
          <SliderRow label="Thickness" value={primary.depth.thickness} min={0.005} max={0.4} step={0.005} defaultValue={0.1} onChange={(v) => editBase('depth.thickness', (l) => ({ ...l, depth: { ...l.depth, thickness: v } }))} />
          <SliderRow label="Bevel" hint="Round-bevel radius — the pill edge that creates lensing." value={primary.depth.bevel} min={0} max={0.2} step={0.001} defaultValue={0.045} onChange={(v) => editBase('depth.bevel', (l) => ({ ...l, depth: { ...l.depth, bevel: v } }))} />
          {bevelClamped && (
            <div className="flex items-start gap-1.5 rounded-md border border-warn/20 bg-warn/[0.07] px-2 py-1.5 text-3xs leading-snug text-warn/90">
              <TriangleAlert className="mt-px h-3 w-3 shrink-0" />
              <span>
                Thin features limit this layer’s bevel to <b className="tabular">{formatNumber(maxBevel!, 3)}</b> — larger values are clamped by the geometry builder.
              </span>
            </div>
          )}
          <SliderRow label="Segments" value={primary.depth.bevelSegments} min={1} max={16} step={1} decimals={0} defaultValue={6} onChange={(v) => editBase('depth.bevelSegments', (l) => ({ ...l, depth: { ...l.depth, bevelSegments: Math.round(v) } }))} />
          <SliderRow label="Inflate" hint="Dome the front face so reflections sweep across it." value={primary.depth.inflate} min={0} max={1} step={0.01} defaultValue={0} onChange={(v) => editBase('depth.inflate', (l) => ({ ...l, depth: { ...l.depth, inflate: v } }))} />
        </Section>

        {/* Transform */}
        <Section
          id="layer.transform"
          title="Position & scale"
          icon={<Move />}
          right={
            <IconButton label="Reset position & scale" size="xs" onClick={() => editBase('transform', (l) => ({ ...l, transform: { x: 0, y: 0, scale: 1 } }))}>
              <RotateCcw />
            </IconButton>
          }
        >
          <SliderRow label="X" value={primary.transform.x} min={-1} max={1} step={0.005} soft origin={0} defaultValue={0} onChange={(v) => editBase('transform.x', (l) => ({ ...l, transform: { ...l.transform, x: v } }))} />
          <SliderRow label="Y" value={primary.transform.y} min={-1} max={1} step={0.005} soft origin={0} defaultValue={0} onChange={(v) => editBase('transform.y', (l) => ({ ...l, transform: { ...l.transform, y: v } }))} />
          <SliderRow label="Scale" value={primary.transform.scale} min={0.1} max={3} step={0.01} scale={100} decimals={0} unit="%" origin={1} defaultValue={1} onChange={(v) => editBase('transform.scale', (l) => ({ ...l, transform: { ...l.transform, scale: v } }))} />
          <p className="pl-[92px] text-3xs text-fg-4">Arrow keys nudge · Shift ×10 · Alt fine</p>
        </Section>

        {/* Shadow */}
        <Section id="layer.shadow" title="Shadow" icon={<SunDim />}>
          <Row label="Kind">
            <Segmented
              size="xs"
              fill
              value={primary.shadow.kind}
              onChange={(v) => editBase('shadow.kind', (l) => ({ ...l, shadow: { ...l.shadow, kind: v } }))}
              options={[
                { value: 'none', label: 'None' },
                { value: 'neutral', label: 'Neutral', tip: 'Soft grey shadow — works on any background' },
                { value: 'chromatic', label: 'Chromatic', tip: 'Spills the layer colour (best on light backgrounds)' },
              ]}
            />
          </Row>
          <SliderRow
            label="Opacity"
            value={primary.shadow.opacity}
            min={0}
            max={1}
            step={0.01}
            scale={100}
            decimals={0}
            unit="%"
            defaultValue={0.5}
            disabled={primary.shadow.kind === 'none'}
            onChange={(v) => editBase('shadow.opacity', (l) => ({ ...l, shadow: { ...l.shadow, opacity: v } }))}
          />
        </Section>

        <Section id="layer.elements" title={`Elements (${primary.elementIds.length})`} icon={<Layers />} defaultCollapsed>
          <ElementList layer={primary} />
        </Section>
      </div>
    </div>
  )
}

function ElementList({ layer }: { layer: Layer }) {
  const project = useEditor((s) => s.project)!
  const selected = useEditor((s) => s.selection.elementIds)
  const map = useMemo(() => new Map(project.elements.map((e) => [e.id, e])), [project.elements])
  return (
    <div className="space-y-0.5">
      {[...layer.elementIds].reverse().map((id) => {
        const el = map.get(id)
        if (!el) return null
        const c = paintColor(el.paint)
        return (
          <button
            key={id}
            type="button"
            onClick={(e) => useEditor.getState().selectElement(id, e.ctrlKey || e.metaKey ? 'toggle' : e.shiftKey ? 'range' : 'replace')}
            className={cn('flex h-6 w-full items-center gap-2 rounded px-1.5 text-left text-2xs', selected.includes(id) ? 'bg-accent-2/[0.12] text-fg' : 'text-fg-3 hover:bg-white/[0.04]')}
          >
            <span className="h-3 w-3 shrink-0 rounded-[3px] border border-white/20" style={{ background: c ?? 'conic-gradient(#f87171,#facc15,#4ade80,#60a5fa,#f87171)' }} />
            <span className="min-w-0 flex-1 truncate">{el.name || el.id}</span>
            <span className="text-3xs text-fg-4">{el.kind === 'image' ? 'image' : el.role}</span>
          </button>
        )
      })}
      <p className="pt-1 text-3xs text-fg-4">Select elements, then use “Move to layer” in the Layers panel (or right-click) to restructure.</p>
    </div>
  )
}
