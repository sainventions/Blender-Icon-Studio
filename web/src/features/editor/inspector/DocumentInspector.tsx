import { Camera, Eraser, ExternalLink, FileCode2, Frame, ImageIcon, Lightbulb, Palette, SquareStack, TriangleAlert } from 'lucide-react'
import type { Fill, PlateShape, Project } from '../../../types'
import { projectsApi } from '../../../api'
import { cn, formatNumber } from '../../../lib/format'
import { lightingCss } from '../../../lib/meta'
import { effectivePlateFill, setPlateFillOverride } from '../../../lib/projectOps'
import { useAppStore } from '../../../store/app'
import { useEditor } from '../../../store/editor'
import { Badge, ColorField, Row, Section, Segmented, Select, SliderRow, Switch, NumberField } from '../../../components/ui'
import { PlatformIcon, ShapeIcon } from '../../../components/icons'
import { SourcePlateBadge } from '../../../components/SourcePlateBadge'
import { LightDial, normalizeAngle } from '../viewportBridge'
import { PLATFORM_IDS, setPlatform } from '../actions'
import { FillEditor } from './FillEditor'
import { MaterialGallery } from './MaterialGallery'
import { PrincipledEditor } from './PrincipledEditor'
import { PlateFillOverrideRows, ScopePicker, useScope } from './scope'
import { LooksSection } from '../../looks/LooksPanels'

const SHAPES: PlateShape[] = ['squircle', 'circle', 'rounded', 'square', 'none']

export function DocumentInspector() {
  const project = useEditor((s) => s.project)!
  const commit = useEditor((s) => s.commit)
  const presets = useAppStore((s) => s.presets.data)!
  const plateScope = useScope('plate')
  const { canvas, lighting, camera } = project
  const plate = canvas.plate
  const platformSpec = presets.platforms[canvas.platform]
  const lightPreset = presets.lighting[lighting.preset]
  const locked = lightPreset?.lockAngle != null

  const edit = (key: string, fn: (p: Project) => Project) => commit(fn, { coalesce: key })
  const setCanvas = (key: string, patch: Partial<Project['canvas']>) => edit(`canvas.${key}`, (p) => ({ ...p, canvas: { ...p.canvas, ...patch } }))
  const setPlate = (key: string, patch: Partial<Project['canvas']['plate']>) =>
    edit(`plate.${key}`, (p) => ({ ...p, canvas: { ...p.canvas, plate: { ...p.canvas.plate, ...patch } } }))
  const setLighting = (key: string, patch: Partial<Project['lighting']>) => edit(`lighting.${key}`, (p) => ({ ...p, lighting: { ...p.lighting, ...patch } }))
  const setCamera = (key: string, patch: Partial<Project['camera']>) => edit(`camera.${key}`, (p) => ({ ...p, camera: { ...p.camera, ...patch } }))
  const setArt = (key: string, patch: Partial<Project['canvas']['art']>) => edit(`art.${key}`, (p) => ({ ...p, canvas: { ...p.canvas, art: { ...p.canvas.art, ...patch } } }))

  const plateFill = plateScope.bucket ? effectivePlateFill(project, project.appearance) : plate.fill
  const setPlateFill = (f: Fill) => {
    if (plateScope.bucket) {
      const b = plateScope.bucket
      edit(`plateFill.${b}`, (p) => setPlateFillOverride(p, b, f))
    } else setPlate('fill', { fill: f })
  }
  const darkOverrides = Object.keys(project.appearances.dark.layers ?? {}).length
  const monoOverrides = Object.keys(project.appearances.mono.layers ?? {}).length

  return (
    <div className="pb-6">
      <LooksSection />
      <Section id="doc.platform" title="Platform & shape" icon={<Frame />}>
        <Row label="Platform">
          <Select
            value={canvas.platform}
            onChange={(v) => setPlatform(v)}
            options={PLATFORM_IDS.map((id) => ({
              value: id,
              label: presets.platforms[id]?.label ?? (id === 'free' ? 'Free-form' : id),
              icon: <PlatformIcon platform={id} className="h-3.5 w-3.5" />,
              description: presets.platforms[id] ? `${presets.platforms[id].canvas} px canvas · ${presets.platforms[id].appearances.length} appearance${presets.platforms[id].appearances.length > 1 ? 's' : ''}` : 'Any shape, no platform rules',
            }))}
            className="flex-1"
          />
        </Row>
        {platformSpec?.note && <p className="pl-[92px] text-3xs leading-snug text-fg-4">{platformSpec.note}</p>}
        <Row label="Shape">
          <Segmented
            size="sm"
            fill
            value={canvas.shape}
            onChange={(v) => setCanvas('shape', { shape: v })}
            options={SHAPES.map((s) => ({ value: s, icon: <ShapeIcon shape={s} />, tip: s === 'none' ? 'No plate' : s[0].toUpperCase() + s.slice(1) }))}
          />
        </Row>
        {canvas.shape === 'rounded' && (
          <SliderRow label="Corner radius" value={canvas.cornerRadius} min={0} max={0.5} step={0.005} defaultValue={0.225} scale={100} decimals={1} unit="%" onChange={(v) => setCanvas('cornerRadius', { cornerRadius: v })} />
        )}
      </Section>

      <Section id="doc.plate" title="Plate" icon={<SquareStack />} right={<ScopePicker section="plate" />}>
        <Row label="Visible">
          <Switch checked={plate.visible} onChange={(v) => setPlate('visible', { visible: v })} label="Plate visible" />
          <span className="text-3xs text-fg-4">{plate.visible ? 'Backplate rendered' : 'Layers float on transparency'}</span>
        </Row>
        <FillEditor
          fill={plateFill}
          types={['solid', 'linear', 'radial', 'system-light', 'system-dark', 'none']}
          onChange={(f) => setPlateFill(f)}
          disabled={!plate.visible}
        />
        {!plateScope.bucket && <PlateFillOverrideRows project={project} />}
        <div className={cn('space-y-[7px]', !plate.visible && 'pointer-events-none opacity-45')}>
          <SliderRow label="Thickness" value={plate.thickness} min={0.01} max={0.5} step={0.005} defaultValue={0.16} onChange={(v) => setPlate('thickness', { thickness: v })} />
          <SliderRow label="Bevel" value={plate.bevel} min={0} max={0.2} step={0.001} defaultValue={0.04} onChange={(v) => setPlate('bevel', { bevel: v })} />
          <div className="pt-1">
            <MaterialGallery
              galleryId="gallery.plate"
              defaultOpen={false}
              value={plate.material.preset}
              presets={presets}
              onChange={(id) => id !== plate.material.preset && setPlate('material', { material: { preset: id, params: {} } })}
            />
          </div>
          <PrincipledEditor presets={presets} spec={plate.material} stateKey="plate" onChange={(spec) => setPlate('material', { material: spec })} />
        </div>
      </Section>

      <Section id="doc.art" title="Artwork placement" icon={<ImageIcon />} defaultCollapsed>
        <SliderRow label="Scale" value={canvas.art.scale} min={0.1} max={4} step={0.01} scale={100} decimals={0} unit="%" origin={1} onChange={(v) => setArt('scale', { scale: v })} />
        <SliderRow label="Offset X" value={canvas.art.x} min={-1} max={1} step={0.005} soft origin={0} onChange={(v) => setArt('x', { x: v })} />
        <SliderRow label="Offset Y" value={canvas.art.y} min={-1} max={1} step={0.005} soft origin={0} onChange={(v) => setArt('y', { y: v })} />
        <p className="pl-[92px] text-3xs leading-snug text-fg-4">Imported icons are fitted so the source plate maps onto the canvas plate.</p>
      </Section>

      <Section id="doc.lighting" title="Lighting" icon={<Lightbulb />}>
        <div className="grid grid-cols-4 gap-1.5">
          {Object.entries(presets.lighting).map(([id, lp]) => (
            <button
              key={id}
              type="button"
              onClick={() => setLighting('preset', lp.lockAngle != null ? { preset: id, angle: lp.lockAngle } : { preset: id })}
              data-tip={lp.description}
              className="group flex flex-col items-center gap-1"
            >
              <span
                className={cn(
                  'block aspect-[4/3] w-full rounded-md border transition-[box-shadow,border-color]',
                  lighting.preset === id ? 'border-transparent shadow-[0_0_0_2px_var(--color-accent)]' : 'border-line-2 group-hover:border-line-3',
                )}
                style={{ background: lightingCss(id, lp) }}
              />
              <span className={cn('w-full truncate text-center text-[9.5px]', lighting.preset === id ? 'text-fg' : 'text-fg-4 group-hover:text-fg-2')}>{lp.label}</span>
            </button>
          ))}
        </div>
        <div className={cn('flex items-center gap-3 rounded-lg border border-line bg-surface-0/50 p-2', locked && 'opacity-50')}>
          <div className={cn(locked && 'pointer-events-none')}>
            <LightDial angle={lighting.angle} size={96} onChange={(deg) => setLighting('angle', { angle: normalizeAngle(deg) })} />
          </div>
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex items-center gap-1.5">
              <span className="text-2xs text-fg-3">Angle</span>
              <NumberField value={lighting.angle} min={-180} max={180} step={1} decimals={0} unit="°" disabled={locked} onChange={(v) => setLighting('angle', { angle: normalizeAngle(v) })} className="w-[64px]" />
            </div>
            <div className="flex flex-wrap gap-1">
              {[
                [-45, 'Top-left'],
                [0, 'Top'],
                [45, 'Top-right'],
                [-90, 'Left'],
              ].map(([a, l]) => (
                <button
                  key={l}
                  type="button"
                  disabled={locked}
                  onClick={() => setLighting('angle', { angle: a as number })}
                  className={cn('h-5 rounded px-1.5 text-3xs transition-colors', lighting.angle === a ? 'bg-accent/20 text-fg' : 'bg-white/[0.04] text-fg-3 hover:bg-white/[0.08]')}
                >
                  {l}
                </button>
              ))}
            </div>
            {locked && <div className="text-3xs text-fg-4">{lightPreset?.label} locks the light to {lightPreset?.lockAngle}°.</div>}
          </div>
        </div>
        <SliderRow label="Elevation" hint="0° = from the camera, 90° = grazing." value={lighting.elevation} min={0} max={90} step={1} decimals={0} unit="°" defaultValue={50} onChange={(v) => setLighting('elevation', { elevation: v })} />
        <SliderRow label="Intensity" value={lighting.intensity} min={0} max={3} step={0.01} defaultValue={1} onChange={(v) => setLighting('intensity', { intensity: v })} />
        <SliderRow label="Rim" value={lighting.rim} min={0} max={3} step={0.01} defaultValue={1} onChange={(v) => setLighting('rim', { rim: v })} />
        <SliderRow label="Fill" value={lighting.fill} min={0} max={3} step={0.01} defaultValue={1} onChange={(v) => setLighting('fill', { fill: v })} />
        <SliderRow label="Environment" value={lighting.environment} min={0} max={3} step={0.01} defaultValue={1} onChange={(v) => setLighting('environment', { environment: v })} />
        <SliderRow label="Softness" hint="Light size: 0 = hard shadows, 1 = very soft." value={lighting.shadowSoftness} min={0} max={1} step={0.01} defaultValue={0.5} onChange={(v) => setLighting('shadowSoftness', { shadowSoftness: v })} />
      </Section>

      <Section id="doc.camera" title="Camera" icon={<Camera />}>
        <Row label="Projection">
          <Segmented
            size="xs"
            fill
            value={camera.view}
            onChange={(v) => setCamera('view', { view: v })}
            options={[
              { value: 'front', label: 'Orthographic', tip: 'Orthographic, like the OS draws icons: head-on or a CAD-style angled view' },
              { value: 'perspective', label: 'Perspective', tip: 'Tilted perspective hero shot' },
            ]}
          />
        </Row>
        {camera.view === 'front' ? (
          <>
            <SliderRow
              label="View angle"
              hint="CAD-style POV: 0 = head-on, 100 % = isometric. Layers keep their real distances; renders and the live view use the same camera."
              value={camera.iso ?? 0}
              min={0}
              max={1}
              step={0.01}
              scale={100}
              decimals={0}
              unit="%"
              defaultValue={0}
              onChange={(v) => setCamera('iso', { iso: v })}
            />
            <div className="flex gap-1 pl-[92px]">
              {(
                [
                  [0, 'Front'],
                  [0.5, 'Three-quarter'],
                  [1, 'Isometric'],
                ] as const
              ).map(([v, l]) => (
                <button
                  key={l}
                  type="button"
                  onClick={() => setCamera('iso', { iso: v })}
                  className={cn('h-5 rounded px-1.5 text-3xs transition-colors', Math.abs((camera.iso ?? 0) - v) < 1e-3 ? 'bg-accent/20 text-fg' : 'bg-white/[0.04] text-fg-3 hover:bg-white/[0.08]')}
                >
                  {l}
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <SliderRow label="Tilt X" value={camera.tiltX} min={-60} max={60} step={0.5} decimals={1} unit="°" origin={0} defaultValue={0} onChange={(v) => setCamera('tiltX', { tiltX: v })} />
            <SliderRow label="Tilt Y" value={camera.tiltY} min={-60} max={60} step={0.5} decimals={1} unit="°" origin={0} defaultValue={0} onChange={(v) => setCamera('tiltY', { tiltY: v })} />
            <SliderRow label="Field of view" value={camera.fov} min={10} max={90} step={1} decimals={0} unit="°" defaultValue={30} onChange={(v) => setCamera('fov', { fov: v })} />
          </>
        )}
        <SliderRow label="Zoom" value={camera.zoom} min={0.5} max={2.5} step={0.01} scale={100} decimals={0} unit="%" defaultValue={1} onChange={(v) => setCamera('zoom', { zoom: v })} />
      </Section>

      <Section id="doc.source" title="Source SVG" icon={<FileCode2 />} defaultCollapsed>
        <div className="flex items-center gap-2.5">
          <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-lg border border-line-2 checkerboard-sm">
            <img src={projectsApi.sourceSvgUrl(project.id)} alt="" className="absolute inset-0 h-full w-full object-contain" />
          </div>
          <div className="min-w-0 flex-1 text-2xs">
            <div className="truncate font-semibold text-fg-2">{project.source.filename}</div>
            <div className="tabular text-fg-4">
              viewBox {project.source.viewBox.map((v) => formatNumber(v, 1)).join(' ')} · {project.elements.length} elements
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              <SourcePlateBadge source={project.source} />
              <Badge tip="Split strategy">{project.strategy}</Badge>
            </div>
          </div>
          <a
            href={projectsApi.sourceSvgUrl(project.id)}
            target="_blank"
            rel="noreferrer"
            data-tip="Open the original SVG"
            className="flex h-7 w-7 items-center justify-center rounded-md text-fg-3 hover:bg-white/[0.07] hover:text-fg"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </div>
        {project.source.warnings.length > 0 && (
          <div className="space-y-1 rounded-md border border-warn/20 bg-warn/[0.06] p-2">
            {project.source.warnings.map((w, i) => (
              <div key={i} className="flex items-start gap-1.5 text-3xs leading-snug text-warn/90">
                <TriangleAlert className="mt-px h-3 w-3 shrink-0" /> {w}
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section id="doc.appearances" title="Appearances" icon={<Palette />}>
        <Row label="Tint colour" hint="Used by the Tinted Light / Tinted Dark renditions.">
          <ColorField
            value={project.appearances.tint.color}
            onChange={(c) => edit('tint.color', (p) => ({ ...p, appearances: { ...p.appearances, tint: { ...p.appearances.tint, color: c } } }))}
            className="flex-1"
          />
        </Row>
        <SliderRow
          label="Tint strength"
          value={project.appearances.tint.strength}
          min={0}
          max={1}
          step={0.01}
          scale={100}
          decimals={0}
          unit="%"
          defaultValue={0.8}
          onChange={(v) => edit('tint.strength', (p) => ({ ...p, appearances: { ...p.appearances, tint: { ...p.appearances.tint, strength: v } } }))}
        />
        <div className="space-y-1 rounded-lg border border-line bg-surface-0/50 p-2 text-2xs">
          {(
            [
              ['dark', 'Dark', darkOverrides, project.appearances.dark.plateFill],
              ['mono', 'Mono (Clear & Tinted)', monoOverrides, project.appearances.mono.plateFill],
            ] as const
          ).map(([b, label, n, pf]) => (
            <div key={b} className="flex items-center gap-2">
              <span className="flex-1 text-fg-3">
                <b className="font-semibold text-fg-2">{label}</b> · {n} layer override{n === 1 ? '' : 's'}
                {pf ? ' · plate fill' : ''}
              </span>
              <button
                type="button"
                disabled={!n && !pf}
                data-tip={`Remove all ${label} overrides`}
                onClick={() => edit(`clear.${b}`, (p) => ({ ...p, appearances: { ...p.appearances, [b]: { plateFill: b === 'dark' ? { type: 'system-dark' } : null, layers: {} } } }))}
                className="flex h-5 w-5 items-center justify-center rounded text-fg-4 hover:bg-white/10 hover:text-fg disabled:opacity-30"
              >
                <Eraser className="h-3 w-3" />
              </button>
            </div>
          ))}
          <p className="pt-0.5 text-3xs leading-snug text-fg-4">Switch appearance (1-6) and set a section’s scope to vary fills, opacity, blend and materials.</p>
        </div>
      </Section>
    </div>
  )
}
