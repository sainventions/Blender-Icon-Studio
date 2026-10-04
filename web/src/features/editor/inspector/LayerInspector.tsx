import { useMemo } from 'react'
import {
  AlignVerticalSpaceAround,
  Blend,
  Box,
  CornerUpLeft,
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
  Shapes,
  SunDim,
} from 'lucide-react'
import type { BlendMode, Fill, Layer, LayerOverride, MaterialSpec, Presets, Project } from '../../../types'
import { projectsApi } from '../../../api'
import { cn, formatNumber, hashString } from '../../../lib/format'
import { effectiveLayer, setLayerOverride, updateLayers, type OverrideField } from '../../../lib/projectOps'
import { paintColor, fillToCss } from '../../../lib/color'
import { isReservedParam } from '../../../lib/looks'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { useUi } from '../../../store/ui'
import { Button, EmptyState, IconButton, Row, Section, Segmented, Select, SliderRow, Switch, type ChangePhase } from '../../../components/ui'
import { FillEditor } from './FillEditor'
import { MaterialGallery, MaterialSwatch } from './MaterialGallery'
import { PrincipledEditor, type MaterialEditKeys } from './PrincipledEditor'
import { overriddenElements, updateElementMaterials } from './principled'
import { bevelLimit, roundnessOf, withRoundness, withThickness } from './depth'
import { restackProject } from '../stacking'
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
        Click a layer in the stack or in the viewport. Ctrl/Shift-click to edit several layers at once; expand a layer
        and click a shape to give it its own material.
      </EmptyState>
    )
  }

  const primary = layers.find((l) => l.id === selection.primary) ?? layers[layers.length - 1]
  const editable = layers.filter((l) => !l.locked)
  const ids = editable.map((l) => l.id)
  const allLocked = ids.length === 0
  const key = ids.join(',')
  // Selected shapes (element rows of the layers panel) of a single selected layer: their own material is edited.
  const shapeIds = layers.length === 1 ? selection.elementIds.filter((id) => primary.elementIds.includes(id)) : []

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

  /** A material *input* edit only touches those inputs, and only on selected layers that use the same preset —
   *  multi-selecting a glass and a chrome layer and dragging "Roughness" must not turn the chrome into glass. */
  const editMaterialParams = (next: MaterialSpec, keys: MaterialEditKeys) => {
    const apply = (m: MaterialSpec) => applyParams(m, next, keys)
    const bucket = materialScope.bucket
    const coalesce = Array.isArray(keys) ? keys.join('+') : '*'
    if (bucket) {
      commit(
        (p) => {
          let out = p
          for (const l of p.layers) {
            if (!ids.includes(l.id)) continue
            const cur = effectiveLayer(p, p.appearance, l).material
            const m = apply(cur)
            if (m !== cur) out = setLayerOverride(out, bucket, [l.id], { material: m })
          }
          return out
        },
        { coalesce: `ov:${bucket}:${key}:material.${coalesce}` },
      )
    } else {
      editBase(`material.${coalesce}`, (l) => {
        const m = apply(l.material)
        return m === l.material ? l : { ...l, material: m }
      })
    }
  }

  const colorView = colorScope.bucket ? effectiveLayer(project, project.appearance, primary) : primary
  const materialView = materialScope.bucket ? effectiveLayer(project, project.appearance, primary) : primary
  const material = materialView.material
  const firstElementColor = paintColor(project.elements.find((e) => primary.elementIds.includes(e.id))?.paint) ?? undefined
  const version = geometry?.layers[primary.id]?.hash ?? hashString(primary.elementIds.join(','))
  const ownShapes = overriddenElements(primary)
  const shapeOwn = shapeIds.length ? (primary.elementMaterials?.[shapeIds[0]] ?? null) : null

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
              : `${primary.elementIds.length} shape${primary.elementIds.length === 1 ? '' : 's'} · z ${formatNumber(primary.depth.z, 2)} · ${primary.mode}${ownShapes.length ? ` · ${ownShapes.length} own material${ownShapes.length === 1 ? '' : 's'}` : ''}`}
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
        {/* Material: ONE Principled BSDF per shape (PLAN §11) */}
        <Section id="layer.material" title="Material" icon={<Gem />} right={shapeOwn ? undefined : <ScopePicker section="material" />}>
          {!primary.glass && (
            <div className="flex items-center gap-2 rounded-md border border-line bg-surface-0/50 px-2 py-1.5 text-3xs text-fg-3">
              <span className="min-w-0 flex-1 leading-snug">This layer renders as a flat inlay (no 3D body or material).</span>
              <Button size="xs" variant="secondary" onClick={() => editBase('glass', (l) => ({ ...l, glass: true }))}>
                Make 3D
              </Button>
            </div>
          )}
          {shapeIds.length > 0 && (
            <ShapeTarget project={project} layer={primary} shapeIds={shapeIds} presets={presets} own={shapeOwn} />
          )}
          {shapeOwn ? (
            <ShapeMaterialEditor layer={primary} shapeIds={shapeIds} own={shapeOwn} presets={presets} />
          ) : (
            <>
              {shapeIds.length > 0 && <div className="!mt-2.5 text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">Layer material</div>}
              <MaterialGallery
                value={material.preset}
                presets={presets}
                onChange={(id) => {
                  if (id !== material.preset) editField(materialScope, 'material', { preset: id, params: {} } as MaterialSpec)
                }}
              />
              <PrincipledEditor
                presets={presets}
                spec={material}
                stateKey={`layer:${primary.id}:${materialScope.bucket ?? 'base'}`}
                onChange={(next, _phase: ChangePhase, keys) => editMaterialParams(next, keys)}
              />
              {!materialScope.bucket && <OverrideRows project={project} layerIds={[primary.id]} field="material" presets={presets} />}
              {!shapeIds.length && ownShapes.length > 0 && !materialScope.bucket && (
                <p className="text-3xs leading-snug text-fg-4">
                  {ownShapes.length} shape{ownShapes.length === 1 ? ' has its' : 's have their'} own material — click it in the Layers panel (expand the layer) to edit it.
                </p>
              )}
            </>
          )}
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
          <Row label="Blend mode" hint="Written to the Apple .icon export. Blender renders composite physically (light through glass), not with 2D blend modes.">
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

        {/* Depth: height-field bodies (PLAN §11 Geometry) */}
        <Section
          id="layer.depth"
          title="Depth"
          icon={<Box />}
          right={
            <IconButton
              label="Re-stack all layers bottom → top at their real heights (thickness + inflated dome), no bodies overlapping"
              size="xs"
              // the shared rule (presets.json "geometry"): z0 = stackLift, z(i+1) = z(i) + H(i) + stackGap
              onClick={() => commit((p) => restackProject(p, geometry, presets), { coalesce: 'restack' })}
            >
              <AlignVerticalSpaceAround />
            </IconButton>
          }
        >
          <SliderRow
            label="Z position"
            hint="Height of the layer's back face above the plate (art units). The camera's View control shows the real distances."
            value={primary.depth.z}
            min={0}
            max={1.2}
            step={0.005}
            soft
            onChange={(v) => editBase('depth.z', (l) => ({ ...l, depth: { ...l.depth, z: v } }))}
          />
          <SliderRow
            label="Thickness"
            hint="Body height (art units). Roundness is kept while you change it."
            value={primary.depth.thickness}
            min={0.005}
            max={0.4}
            step={0.005}
            defaultValue={0.1}
            onChange={(v) => editBase('depth.thickness', (l) => withThickness(l, v))}
          />
          <SliderRow
            label="Roundness"
            hint="Round-edge radius as a share of half the thickness. 100 % = a full pill edge; thinner parts and tips taper on their own. Add Inflate for a lens or sphere."
            value={roundnessOf(primary)}
            min={0}
            max={1}
            step={0.01}
            scale={100}
            decimals={0}
            unit="%"
            onChange={(v) => editBase('depth.bevel', (l) => withRoundness(l, v))}
          />
          <SliderRow
            label="Inflate"
            hint="Domes the faces: height grows with the shape's width while thin parts and tips taper, so nothing self-intersects."
            value={primary.depth.inflate}
            min={0}
            max={1}
            step={0.01}
            defaultValue={0}
            onChange={(v) => editBase('depth.inflate', (l) => ({ ...l, depth: { ...l.depth, inflate: v } }))}
          />
          <SliderRow
            label="Segments"
            hint="Rings across the round edge and the dome (smoothness)."
            value={primary.depth.bevelSegments}
            min={1}
            max={16}
            step={1}
            decimals={0}
            defaultValue={6}
            onChange={(v) => editBase('depth.bevelSegments', (l) => ({ ...l, depth: { ...l.depth, bevelSegments: Math.round(v) } }))}
          />
          <p className="pl-[92px] text-3xs tabular text-fg-4">
            Edge radius {formatNumber(primary.depth.bevel, 3)} · max {formatNumber(bevelLimit(primary), 3)}
          </p>
          <Row label="Bodies" hint="Individual: every shape is its own body (and may have its own material). Combined: one body around the union.">
            <Segmented
              size="xs"
              fill
              value={primary.mode}
              onChange={(v) => editBase('mode', (l) => ({ ...l, mode: v }))}
              options={[
                { value: 'individual', label: 'Individual', tip: 'Every shape is its own body' },
                { value: 'combined', label: 'Combined', tip: 'One body around the union of the shapes' },
              ]}
            />
          </Row>
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

        {/* Shadow: real only (PLAN §11) */}
        <Section id="layer.shadow" title="Shadow" icon={<SunDim />}>
          <Row label="Casts" hint="Cycles traces real shadows: glass casts a lit, coloured shadow; solids a soft dark one.">
            <Segmented
              size="xs"
              fill
              value={primary.shadow.kind === 'none' ? 'none' : 'physical'}
              onChange={(v) => editBase('shadow.kind', (l) => (l.shadow.kind === v ? l : { ...l, shadow: { ...l.shadow, kind: v } }))}
              options={[
                { value: 'physical', label: 'Physical', tip: 'The real shadow Cycles traces from the lights' },
                { value: 'none', label: 'None', tip: 'This layer casts no shadow (it still receives them)' },
              ]}
            />
          </Row>
        </Section>

        <Section id="layer.elements" title={`Shapes (${primary.elementIds.length})`} icon={<Layers />} defaultCollapsed>
          <ElementList layer={primary} presets={presets} />
        </Section>
      </div>
    </div>
  )
}

/** Apply the inputs `keys` of `next` onto `m` (same preset only); '*' resets every (non-reserved) input. */
function applyParams(m: MaterialSpec, next: MaterialSpec, keys: MaterialEditKeys): MaterialSpec {
  if (m.preset !== next.preset) return m
  if (keys === '*') return Object.keys(m.params ?? {}).some((k) => !isReservedParam(k)) ? { ...m, params: {} } : m
  let params: MaterialSpec['params'] | null = null
  for (const k of keys) {
    const v = next.params?.[k]
    if ((m.params ?? {})[k] === v) continue
    params ??= { ...(m.params ?? {}) }
    if (v === undefined) delete params[k]
    else params[k] = v
  }
  return params ? { ...m, params } : m
}

// ------------------------------------------------------------------------------------------ per-shape materials
/** "This shape: inherit / own material" for the shapes selected in the layers panel (Layer.elementMaterials). */
function ShapeTarget({
  project,
  layer,
  shapeIds,
  presets,
  own,
}: {
  project: Project
  layer: Layer
  shapeIds: string[]
  presets: Presets
  own: MaterialSpec | null
}) {
  const commit = useEditor((s) => s.commit)
  const el = project.elements.find((e) => e.id === shapeIds[0])
  const name = shapeIds.length > 1 ? `${shapeIds.length} shapes` : (el?.name || el?.id || shapeIds[0])
  const mixed = shapeIds.some((id) => !!layer.elementMaterials?.[id] !== !!own)
  const setMode = (mode: 'inherit' | 'own') =>
    commit(
      (p) => ({
        ...p,
        layers: p.layers.map((l) =>
          l.id !== layer.id
            ? l
            : updateElementMaterials(l, shapeIds, (cur) => (mode === 'inherit' ? undefined : (cur ?? { preset: l.material.preset, params: {} }))),
        ),
      }),
      { coalesce: `shape-mode:${layer.id}:${shapeIds.join(',')}` },
    )
  const appearance = project.appearance
  return (
    <div className="space-y-1.5 rounded-lg border border-accent/25 bg-accent/[0.05] p-2" data-shape-target="">
      <div className="flex items-center gap-2">
        <span className="relative h-5 w-5 shrink-0 overflow-hidden rounded-[5px] border border-white/20 checkerboard-sm">
          <span className="absolute inset-0" style={{ background: el ? fillToCss(el.paint, '#555') : '#555' }} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-2xs font-semibold text-fg">
            <Shapes className="mr-1 inline h-3 w-3 align-[-2px] text-fg-3" />
            {name}
          </div>
          <div className="truncate text-3xs text-fg-4">Shape in “{layer.name}”</div>
        </div>
        <IconButton label="Back to the layer" size="xs" onClick={() => useEditor.getState().selectLayer(layer.id)}>
          <CornerUpLeft />
        </IconButton>
      </div>
      <Segmented<'inherit' | 'own'>
        size="xs"
        fill
        value={mixed ? null : own ? 'own' : 'inherit'}
        onChange={setMode}
        options={[
          { value: 'inherit', label: 'Inherit layer', tip: 'Render with the layer material' },
          { value: 'own', label: 'Own material', tip: 'Give this shape its own Principled BSDF (starts as a copy of the layer material)' },
        ]}
      />
      {layer.mode === 'combined' && own && (
        <div className="flex items-center gap-2 text-3xs leading-snug text-warn/90">
          <span className="min-w-0 flex-1">Combined layers render as one body, which uses the layer material.</span>
          <Button size="xs" variant="secondary" onClick={() => commit((p) => updateLayers(p, [layer.id], (l) => ({ ...l, mode: 'individual' })))}>
            Make individual
          </Button>
        </div>
      )}
      {own && (appearance === 'clear-light' || appearance === 'clear-dark' || appearance === 'tinted-light' || appearance === 'tinted-dark') && (
        <p className="text-3xs leading-snug text-fg-4">Clear and Tinted renditions draw every shape with one glass look.</p>
      )}
      {!own && !mixed && (
        <p className="text-3xs leading-snug text-fg-4">
          Inherits <b className="font-semibold text-fg-3">{presets.materials[layer.material.preset]?.label ?? layer.material.preset}</b> from the layer —
          edits below change the whole layer.
        </p>
      )}
    </div>
  )
}

/** Editor of the selected shapes' own material: same preset → only the inputs it changes (the rest follows the
 *  layer); another preset → that preset's starting values (blender_worker presets.resolve_material). */
function ShapeMaterialEditor({ layer, shapeIds, own, presets }: { layer: Layer; shapeIds: string[]; own: MaterialSpec; presets: Presets }) {
  const commit = useEditor((s) => s.commit)
  const key = `${layer.id}:${shapeIds.join(',')}`
  const write = (field: string, fn: (cur: MaterialSpec) => MaterialSpec) =>
    commit(
      (p) => ({
        ...p,
        layers: p.layers.map((l) => (l.id !== layer.id ? l : updateElementMaterials(l, shapeIds, (cur) => (cur ? fn(cur) : cur)))),
      }),
      { coalesce: `shape:${key}:${field}` },
    )
  const sameAsLayer = own.preset === layer.material.preset
  return (
    <>
      <MaterialGallery
        galleryId="gallery.shape"
        value={own.preset}
        presets={presets}
        onChange={(id) => id !== own.preset && write('preset', () => ({ preset: id, params: {} }))}
      />
      <MaterialSwatchNote layer={layer} own={own} presets={presets} sameAsLayer={sameAsLayer} />
      <PrincipledEditor
        presets={presets}
        spec={own}
        inherited={layer.material}
        resetTo={sameAsLayer ? 'layer' : 'preset'}
        stateKey={`shape:${key}`}
        onChange={(next, _phase, keys) => write(Array.isArray(keys) ? keys.join('+') : '*', (cur) => applyParams(cur, next, keys))}
      />
    </>
  )
}

function MaterialSwatchNote({ layer, own, presets, sameAsLayer }: { layer: Layer; own: MaterialSpec; presets: Presets; sameAsLayer: boolean }) {
  const layerPreset = presets.materials[layer.material.preset]
  return (
    <div className="flex items-center gap-1.5 text-3xs leading-snug text-fg-4">
      <MaterialSwatch id={layer.material.preset} preset={layerPreset} className="relative h-3.5 w-3.5 shrink-0 rounded-full" />
      <span className="min-w-0 flex-1">
        {sameAsLayer
          ? `Inputs you don't change follow the layer's ${layerPreset?.label ?? layer.material.preset}.`
          : `Starts from ${presets.materials[own.preset]?.label ?? own.preset}'s values (the layer is ${layerPreset?.label ?? layer.material.preset}).`}
      </span>
    </div>
  )
}

function ElementList({ layer, presets }: { layer: Layer; presets: Presets }) {
  const project = useEditor((s) => s.project)!
  const selected = useEditor((s) => s.selection.elementIds)
  const map = useMemo(() => new Map(project.elements.map((e) => [e.id, e])), [project.elements])
  return (
    <div className="space-y-0.5">
      {[...layer.elementIds].reverse().map((id) => {
        const el = map.get(id)
        if (!el) return null
        const c = paintColor(el.paint)
        const own = layer.elementMaterials?.[id]
        return (
          <button
            key={id}
            type="button"
            onClick={(e) => useEditor.getState().selectElement(id, e.ctrlKey || e.metaKey ? 'toggle' : e.shiftKey ? 'range' : 'replace')}
            className={cn('flex h-6 w-full items-center gap-2 rounded px-1.5 text-left text-2xs', selected.includes(id) ? 'bg-accent-2/[0.12] text-fg' : 'text-fg-3 hover:bg-white/[0.04]')}
          >
            <span className="h-3 w-3 shrink-0 rounded-[3px] border border-white/20" style={{ background: c ?? 'conic-gradient(#f87171,#facc15,#4ade80,#60a5fa,#f87171)' }} />
            <span className="min-w-0 flex-1 truncate">{el.name || el.id}</span>
            {own && (
              <span className="flex shrink-0 items-center gap-1 text-3xs text-fg-3" data-tip={`Own material: ${presets.materials[own.preset]?.label ?? own.preset}`}>
                <MaterialSwatch id={own.preset} preset={presets.materials[own.preset]} className="relative h-2.5 w-2.5 rounded-full" />
              </span>
            )}
            <span className="text-3xs text-fg-4">{el.kind === 'image' ? 'image' : el.role}</span>
          </button>
        )
      })}
      <p className="pt-1 text-3xs text-fg-4">Click a shape to give it its own material · “Move to layer” in the Layers panel (or right-click) restructures.</p>
    </div>
  )
}
