// Everything inside the <Canvas>: backdrop, studio lighting, camera rig, plate, layer stack, grid, post FX.
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { AppearanceId, Fill, GeometryBundle, LayerTransform, Presets, Project } from '../../types'
import { appearanceWallpaper, isDarkAppearance } from '../../lib/appearance'
import { plateOutline } from '../../lib/shapes'
import { liquidGlassLit, type FakeGlassBinding } from '../../lib/materials3d'
import { Backdrop, useBackdropBinding, type BackdropSpec } from './Backdrop'
import { CameraRig } from './CameraRig'
import { Effects } from './Effects'
import { GridOverlay } from './GridOverlay'
import { LayerBody, buildStack, iconLumRange, stackFramePoints, stackTop, type PlateFrame } from './LayerStack'
import { Plate } from './Plate'
import { lightDir, resolveRig } from './rig'
import { StudioLighting } from './StudioLighting'
import { damp, useViewportStore } from './store'
import { fillPreviewColor, usePaint } from './usePaint'

export interface SceneRootProps {
  /** Effective (appearance-resolved) project. */
  project: Project
  appearance: AppearanceId
  geometry: GeometryBundle | null
  presets: Presets | null
  selectedLayerId: string | null
  onSelectLayer: (id: string | null) => void
  onLayerTransform?: (id: string, t: LayerTransform) => void
  explode: number
  view: 'front' | 'orbit'
  showGrid: boolean
}

function ExplodeDriver({ target }: { target: number }) {
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  useEffect(() => {
    store.explode.target = Math.max(0, Math.min(1, Number.isFinite(target) ? target : 0))
    invalidate()
  }, [store, target, invalidate])
  useFrame((state, dt) => {
    const ex = store.explode
    if (ex.current === ex.target) return
    const next = damp(ex.current, ex.target, 7, dt)
    ex.current = Math.abs(next - ex.target) < 5e-4 ? ex.target : next
    state.invalidate()
  }, -2)
  return null
}

/**
 * Like the worker: clear / tinted renditions always have their wallpaper behind the icon (glass refracts it); a
 * 'wallpaper' backdrop shows it for every rendition. An explicit colour backdrop wins; 'transparent' = checker,
 * except where the wallpaper is part of the rendition (it is what the frosted plate shows through itself).
 */
function backdropSpec(project: Project, appearance: AppearanceId): BackdropSpec {
  const r = project.render
  if (r.backdrop === 'color') return { kind: 'color', color: r.backdropColor || '#1c1c22' }
  const wall = appearanceWallpaper(appearance)
  if (wall) return { kind: 'wallpaper', tone: wall }
  if (r.backdrop === 'wallpaper') return { kind: 'wallpaper', tone: isDarkAppearance(appearance) ? 'dark' : 'light' }
  return { kind: 'checker' }
}

export function SceneRoot(p: SceneRootProps) {
  const { project, geometry, presets } = p
  const { canvas, lighting } = project
  const gl = useThree((s) => s.gl)

  // Stable callback identities so memoised layer bodies do not re-render on every parent render.
  const selectRef = useRef(p.onSelectLayer)
  selectRef.current = p.onSelectLayer
  const transformRef = useRef(p.onLayerTransform)
  transformRef.current = p.onLayerTransform
  const onSelect = useCallback((id: string | null) => selectRef.current(id), [])
  const onTransform = useCallback((id: string, t: LayerTransform) => transformRef.current?.(id, t), [])

  const rig = useMemo(() => resolveRig(lighting, presets), [lighting, presets])
  const rimDir = useMemo(() => lightDir(rig.angle, rig.elevation), [rig.angle, rig.elevation])
  const rimColor = useMemo(() => new THREE.Color(rig.rimColors[0] ?? '#ffffff'), [rig.rimColors])
  // Worker `lit`: Liquid Glass self-illumination follows the key light (dark renditions: key × 0.85).
  const lit = liquidGlassLit(rig.key)

  // Backdrop + plate paint (shared: the plate fill is what lower "fake glass" layers show through themselves).
  const bspec = backdropSpec(project, p.appearance)
  const backdrop = useBackdropBinding(bspec)
  const rawPlateFill = canvas.plate.fill
  const plateFill = useMemo<Fill>(
    () => (rawPlateFill.type === 'auto' ? { type: 'solid', color: '#ffffff', opacity: 1 } : rawPlateFill),
    [rawPlateFill],
  )
  const platePaint = usePaint(plateFill, null, fillPreviewColor(plateFill, '#ffffff'))
  const plateVisible = canvas.plate.visible && canvas.shape !== 'none' && !platePaint.none

  const backdropBehind = backdrop.behind
  const plateBehind = useMemo<FakeGlassBinding>(() => {
    if (!plateVisible) return backdropBehind
    return { map: platePaint.binding.map, color: platePaint.binding.color.clone(), space: 'canvas' }
  }, [plateVisible, platePaint, backdropBehind])

  const stack = useMemo(
    () => buildStack(project.layers, geometry?.layers, canvas.art, project.camera.explode ?? 1, presets),
    [project.layers, geometry, canvas.art, project.camera.explode, presets],
  )
  const top = useCallback((e: number) => stackTop(stack, e), [stack])
  const lumRange = useMemo(() => iconLumRange(stack), [stack])
  const intentLum = useMemo<[number, number]>(() => lumRange, [lumRange[0], lumRange[1]]) // eslint-disable-line react-hooks/exhaustive-deps
  // Camera framing hull: the plate outline (front + back face) and every layer box at a given explode amount.
  const plateFrame = useMemo<PlateFrame | null>(
    () =>
      plateVisible
        ? { outline: plateOutline(canvas.shape, canvas.cornerRadius, 64), thickness: canvas.plate.thickness }
        : null,
    [plateVisible, canvas.shape, canvas.cornerRadius, canvas.plate.thickness],
  )
  const framePoints = useCallback(
    (e: number, out: number[]) => stackFramePoints(stack, e, plateFrame, out),
    [stack, plateFrame],
  )
  // Worker _shadow_color: a shadow ray loses opacity × 0.85 (glass) / 0.95 (solids) of the light, × 0.6 for unlit
  // `flat` layers. One shadow map serves all layers, so the strongest caster sets the darkness.
  const shadowStrength = useMemo(() => {
    let k = 0
    for (const s of stack) {
      if (s.layer.shadow.kind === 'none') continue
      const op = Math.max(0, Math.min(1, s.layer.shadow.opacity))
      k = Math.max(k, op * (s.spec.transmission > 0 ? 0.85 : 0.95) * (s.spec.unlit ? 0.6 : 1))
    }
    return k
  }, [stack])
  const bloom = useMemo(() => Math.max(0, ...stack.map((s) => s.spec.bloom)), [stack])
  const bloomIds = useMemo(() => stack.filter((s) => s.spec.bloom > 0).map((s) => s.layer.id), [stack])
  const dpr = gl.getPixelRatio()

  return (
    <>
      <ExplodeDriver target={p.explode} />
      <Backdrop binding={backdrop} zoom={project.camera.zoom || 1} />
      <StudioLighting rig={rig} shadowStrength={shadowStrength} shadows={shadowStrength > 0} />
      <CameraRig
        view={p.view}
        zoom={project.camera.zoom || 1}
        fov={project.camera.fov || 30}
        points={framePoints}
      />
      <group name="icon">
        {plateVisible && (
          <Plate
            canvas={canvas}
            presets={presets}
            paint={platePaint}
            behind={backdropBehind}
            rimDir={rimDir}
            lit={lit}
          />
        )}
        {stack.map((entry) => (
          <LayerBody
            key={entry.layer.id}
            entry={entry}
            art={canvas.art}
            plateBehind={plateBehind}
            rimDir={rimDir}
            rimColor={rimColor}
            view={p.view}
            draggable={!!p.onLayerTransform}
            onSelect={onSelect}
            onTransform={onTransform}
            intentLum={intentLum}
            presets={presets}
            lit={lit}
          />
        ))}
      </group>
      {p.showGrid && (
        <GridOverlay
          shape={canvas.shape}
          cornerRadius={canvas.cornerRadius}
          platform={canvas.platform}
          z={stackTop(stack, 0) + 0.01}
          stackTop={top}
        />
      )}
      <Effects
        colorMode={project.render.colorMode}
        selectedId={p.selectedLayerId}
        bloom={bloom}
        bloomIds={bloomIds}
        multisampling={dpr >= 2 ? 0 : 4}
      />
    </>
  )
}
