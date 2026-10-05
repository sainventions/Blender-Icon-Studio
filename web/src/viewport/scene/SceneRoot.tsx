// Everything inside the <Canvas>: backdrop, studio lighting, camera rig (CAD iso view / orbit), plate, layer stack,
// grid, post FX. Shapes are physical height-field bodies with ONE Principled material each (PLAN §11).
import { useCallback, useMemo, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { AppearanceId, Fill, GeometryBundle, LayerTransform, Presets, Project } from '../../types'
import { appearanceWallpaper, isDarkAppearance } from '../../lib/appearance'
import { plateOutline } from '../../lib/shapes'
import { bloomAmount, isTransmissive, resolveMaterial, type MonoParams } from '../../lib/materials3d'
import { Backdrop, useBackdropBinding, wallpaperColor, type BackdropSpec, type StageElementGetter } from './Backdrop'
import { CameraRig } from './CameraRig'
import { displayTransformFor } from './displayTransform'
import { Effects } from './Effects'
import { GridOverlay } from './GridOverlay'
import { LayerBody, buildStack, stackFramePoints, stackTop, type PlateFrame, type StackEntry } from './LayerStack'
import { Plate } from './Plate'
import { resolveRig } from './rig'
import { StudioLighting } from './StudioLighting'
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
  /** CAD iso view amount (0 head-on .. 1 isometric). */
  iso: number
  view: 'front' | 'orbit'
  showGrid: boolean
  /** Tinted renditions: the worker's env mono (lib/appearance.appearanceMono). */
  mono: MonoParams | null
  /** Element whose CSS background the transparent (checker) backdrop continues; see Backdrop.tsx. */
  stage?: StageElementGetter
}

/**
 * Like the worker: clear / tinted renditions always have their wallpaper behind the icon (glass refracts it); a
 * 'wallpaper' backdrop shows it for every rendition. An explicit colour backdrop wins; 'transparent' = checker.
 */
function backdropSpec(project: Project, appearance: AppearanceId): BackdropSpec {
  const r = project.render
  if (r.backdrop === 'color') return { kind: 'color', color: r.backdropColor || '#1c1c22' }
  const wall = appearanceWallpaper(appearance)
  if (wall) return { kind: 'wallpaper', tone: wall }
  if (r.backdrop === 'wallpaper') return { kind: 'wallpaper', tone: isDarkAppearance(appearance) ? 'dark' : 'light' }
  return { kind: 'checker' }
}

/**
 * Live shadow darkness of one layer (the key's VSM map is grey and shared): Cycles casts a real shadow whose glass
 * share is partly filled by the light the body transmits (caustics are on in every tier), so transmissive casters
 * cast lighter shadows. `none` casts nothing (worker: visible_shadow False).
 */
export function shadowAmount(entry: StackEntry): number {
  if (entry.layer.shadow?.kind === 'none') return 0
  let t = 0
  for (const p of entry.params.values()) t = Math.max(t, p.transmission)
  return 1 - 0.45 * t
}

export function SceneRoot(p: SceneRootProps) {
  const { project, geometry, presets } = p
  const { canvas, lighting } = project
  const dpr = useThree((s) => s.gl.getPixelRatio())

  // Stable callback identities so memoised layer bodies do not re-render on every parent render.
  const selectRef = useRef(p.onSelectLayer)
  selectRef.current = p.onSelectLayer
  const transformRef = useRef(p.onLayerTransform)
  transformRef.current = p.onLayerTransform
  const onSelect = useCallback((id: string | null) => selectRef.current(id), [])
  const onTransform = useCallback((id: string, t: LayerTransform) => transformRef.current?.(id, t), [])

  const rig = useMemo(() => resolveRig(lighting, presets), [lighting, presets])
  const bspec = backdropSpec(project, p.appearance)
  const display = useMemo(() => displayTransformFor(project.render.colorMode), [project.render.colorMode])
  const backdrop = useBackdropBinding(bspec, display)
  const rawPlateFill = canvas.plate.fill
  const plateFill = useMemo<Fill>(
    () => (rawPlateFill.type === 'auto' ? { type: 'solid', color: '#ffffff', opacity: 1 } : rawPlateFill),
    [rawPlateFill],
  )
  const platePaint = usePaint(plateFill, null, fillPreviewColor(plateFill, '#ffffff'))
  const plateVisible = canvas.plate.visible && canvas.shape !== 'none' && !platePaint.none

  const stack = useMemo(
    () => buildStack(project.layers, geometry?.layers, canvas.art, presets, plateVisible),
    [project.layers, geometry, canvas.art, presets, plateVisible],
  )
  const plateParams = useMemo(() => resolveMaterial(canvas.plate.material, null, presets).params, [canvas.plate.material, presets])

  // What covered glass shows through itself (three.js has no transmission through transmission): under the plate the
  // backdrop (the rendition wallpaper is kept under a glass plate even when the backdrop is a colour); under the layers
  // the plate: its paint, or for a glass plate its base colour over that backdrop.
  const wall = appearanceWallpaper(p.appearance)
  const plateGlass = plateVisible && isTransmissive(plateParams)
  const glassLayers = stack.some((e) => e.transmissive)
  const behindPlate = useMemo(
    () => (bspec.kind === 'color' && wall ? wallpaperColor(wall) : backdrop.color.clone()),
    [bspec.kind, wall, backdrop.color],
  )
  const plateCovered = plateGlass && glassLayers ? behindPlate : null
  const behindLayers = useMemo(() => {
    if (!plateVisible) return backdrop.color.clone()
    const paint = platePaint.binding.color
    if (!plateGlass) return paint.clone()
    const base = new THREE.Color(1, 1, 1).lerp(paint, Math.max(0, Math.min(1, plateParams.tint)))
    return base.multiply(behindPlate)
  }, [plateVisible, plateGlass, platePaint, plateParams.tint, behindPlate, backdrop.color])

  // Camera framing hull: the plate outline (front + back face) or the canvas square, and every layer's hull at its
  // real z span (worker subject_points).
  const plateFrame = useMemo<PlateFrame | null>(
    () =>
      plateVisible
        ? { outline: plateOutline(canvas.shape, canvas.cornerRadius, 64), thickness: canvas.plate.thickness }
        : null,
    [plateVisible, canvas.shape, canvas.cornerRadius, canvas.plate.thickness],
  )
  const framePoints = useCallback((out: number[]) => stackFramePoints(stack, plateFrame, out), [stack, plateFrame])
  const shadowIntensity = useMemo(() => {
    let k = 0
    for (const s of stack) k = Math.max(k, shadowAmount(s))
    return k
  }, [stack])
  const casterHeight = useMemo(() => {
    let h = 0
    for (const s of stack) if (shadowAmount(s) > 0) h = Math.max(h, s.height)
    return h
  }, [stack])
  const bloom = useMemo(() => bloomAmount(stack.flatMap((s) => [...s.params.values()])), [stack])
  const bloomIds = useMemo(
    () =>
      stack
        .filter((s) => [...s.params.values()].some((q) => q.paintMode !== 'base' && q.emissionStrength > 1))
        .map((s) => s.layer.id),
    [stack],
  )

  return (
    <>
      <Backdrop binding={backdrop} zoom={project.camera.zoom || 1} display={display} stage={p.stage} />
      <StudioLighting rig={rig} shadowIntensity={shadowIntensity} casterHeight={casterHeight} />
      <CameraRig
        view={p.view}
        zoom={project.camera.zoom || 1}
        fov={project.camera.fov || 30}
        iso={p.iso}
        points={framePoints}
      />
      <group name="icon">
        {plateVisible && <Plate canvas={canvas} presets={presets} paint={platePaint} covered={plateCovered} />}
        {stack.map((entry) => (
          <LayerBody
            key={entry.layer.id}
            entry={entry}
            art={canvas.art}
            view={p.view}
            draggable={!!p.onLayerTransform}
            onSelect={onSelect}
            onTransform={onTransform}
            mono={p.mono}
            behind={behindLayers}
          />
        ))}
      </group>
      {p.showGrid && (
        <GridOverlay shape={canvas.shape} cornerRadius={canvas.cornerRadius} platform={canvas.platform} z={stackTop(stack) + 0.01} />
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
