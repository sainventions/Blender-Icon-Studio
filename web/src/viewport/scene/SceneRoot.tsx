// Everything inside the <Canvas>: backdrop, studio lighting, camera rig, plate, layer stack, grid, post FX.
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { AppearanceId, Fill, GeometryBundle, LayerTransform, Paint, Presets, Project } from '../../types'
import { appearanceWallpaper, isDarkAppearance, monoLutsFor } from '../../lib/appearance'
import { plateOutline } from '../../lib/shapes'
import { liquidGlassLit, monoLutTexture, paintTransformFor, type FakeGlassBinding } from '../../lib/materials3d'
import { filmParamsFor, flushParams } from '../../lib/overlay3d'
import { touchingOpaque } from '../geometry/layerGeometry'
import { Backdrop, useBackdropBinding, useWallpaperBehind, type BackdropSpec, type StageElementGetter } from './Backdrop'
import { CameraRig } from './CameraRig'
import { colorModeId, displayTransformFor } from './displayTransform'
import { Effects } from './Effects'
import { GridOverlay } from './GridOverlay'
import {
  LayerBody,
  buildStack,
  iconLumRange,
  stackFramePoints,
  stackTop,
  type MonoLutTextures,
  type PlateFrame,
  type StackEntry,
} from './LayerStack'
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
  /** Element whose CSS background the transparent (checker) backdrop continues — see Backdrop.tsx. */
  stage?: StageElementGetter
}

/** Representative linear paint of a layer (fill override or the mean of its region paints), as the worker's paint_rgb. */
function layerPaintLinear(entry: StackEntry): [number, number, number] {
  const f = entry.layer.fill
  const cols: string[] = []
  const add = (p: Fill | Paint | null | undefined) => {
    if (!p) return
    if (p.type === 'solid') cols.push(p.color)
    else if (p.type === 'linear' || p.type === 'radial') p.stops.forEach((st) => cols.push(st.color))
  }
  if (f.type === 'auto') entry.lg.regions.forEach((r) => add(r.paint))
  else add(f)
  if (!cols.length) return [1, 1, 1]
  const c = new THREE.Color()
  const sum = [0, 0, 0]
  for (const h of cols) {
    c.setStyle(h)
    sum[0] += c.r
    sum[1] += c.g
    sum[2] += c.b
  }
  return [sum[0] / cols.length, sum[1] / cols.length, sum[2] / cols.length]
}

/** Worker _shadow_color as a grey shadow strength: opacity × (1 − Y(target)) (see shadowStrength). */
export function shadowAmount(entry: StackEntry): number {
  const sh = entry.layer.shadow
  if (sh.kind === 'none') return 0
  let op = Math.max(0, Math.min(1, sh.opacity))
  if (entry.spec.unlit) op *= 0.6
  const strength = entry.spec.transmission > 0 ? 0.85 : 0.95
  let y = 1 - strength
  if (sh.kind === 'chromatic') {
    // HSV(hue, saturation, 0.72) of the paint = the paint scaled to a peak of 0.72 (black: grey 0.72)
    const c = layerPaintLinear(entry)
    const mx = Math.max(...c)
    const t = mx > 1e-6 ? c.map((v) => (0.72 * v) / mx) : [0.72, 0.72, 0.72]
    y = 0.2126 * t[0] + 0.7152 * t[1] + 0.0722 * t[2]
  }
  return op * (1 - y)
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
  const display = useMemo(() => displayTransformFor(project.render.colorMode), [project.render.colorMode])
  // Worker display_paint: paints are pre-compensated for the colour mode's view transform ('brand': the inverse soft
  // clip; 'neutral': inverse Khronos PBR Neutral). Missing / unknown modes are the worker's default, 'brand'.
  const displayPaint = paintTransformFor(project.render.colorMode)
  const backdrop = useBackdropBinding(bspec, display)
  const rawPlateFill = canvas.plate.fill
  const plateFill = useMemo<Fill>(
    () => (rawPlateFill.type === 'auto' ? { type: 'solid', color: '#ffffff', opacity: 1 } : rawPlateFill),
    [rawPlateFill],
  )
  const platePaint = usePaint(plateFill, null, fillPreviewColor(plateFill, '#ffffff'))
  const plateVisible = canvas.plate.visible && canvas.shape !== 'none' && !platePaint.none

  const backdropBehind = backdrop.behind
  // Like the worker, a clear / tinted rendition keeps its wallpaper under a glass plate even when the backdrop is an
  // explicit colour (only the background outside the plate takes the colour).
  const plateWall = useWallpaperBehind(bspec.kind === 'color' ? appearanceWallpaper(p.appearance) : null)
  const plateBackdrop = plateWall ?? backdropBehind
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
  // Round 5 mono maps (worker env mono / monoCombined `lut`) of the clear / tinted renditions, over every visible paint.
  const effAppearance = canvas.platform === 'watchos' ? 'light' : p.appearance // watchOS ignores appearances
  const monoLuts = useMemo<MonoLutTextures | null>(() => {
    const luts = monoLutsFor(effAppearance, project.layers, geometry?.layers)
    return luts ? { mono: monoLutTexture(luts.mono), combined: monoLutTexture(luts.combined) } : null
  }, [effAppearance, project.layers, geometry])
  // Round 5 (worker scene._flush_params / _film_params): Liquid Glass flush with the plate outline, and translucent
  // Liquid Glass pieces as display-space blend films (light / dark renditions).
  const flushShape = plateVisible ? canvas.shape : 'none'
  const flush = useMemo(
    () => flushParams(project.layers, geometry?.layers, canvas, flushShape),
    [project.layers, geometry, canvas, flushShape],
  )
  const cm = colorModeId(project.render.colorMode)
  const films = useMemo(
    () => filmParamsFor(project, geometry?.layers, cm, monoLuts !== null, touchingOpaque, canvas.shape),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [project.layers, project.canvas, geometry, cm, monoLuts],
  )
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
  // Worker _shadow_color: a shadow ray loses opacity × (1 − luminance of the target transmittance) of the light —
  // neutral: 0.85 (glass) / 0.95 (solids); chromatic: the paint's hue at HSV value 0.72 (light paints cast faint
  // coloured shadows) — × 0.6 for unlit `flat` layers. One (grey) shadow map serves all layers: the strongest caster
  // sets the darkness.
  const shadowStrength = useMemo(() => {
    let k = 0
    for (const s of stack) k = Math.max(k, shadowAmount(s))
    return k
  }, [stack])
  const bloom = useMemo(() => Math.max(0, ...stack.map((s) => s.spec.bloom)), [stack])
  const bloomIds = useMemo(() => stack.filter((s) => s.spec.bloom > 0).map((s) => s.layer.id), [stack])
  const dpr = gl.getPixelRatio()

  return (
    <>
      <ExplodeDriver target={p.explode} />
      <Backdrop binding={backdrop} zoom={project.camera.zoom || 1} display={display} stage={p.stage} />
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
            behind={plateBackdrop}
            rimDir={rimDir}
            lit={lit}
            displayPaint={displayPaint}
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
            monoLuts={monoLuts}
            flush={flush.get(entry.layer.id) ?? null}
            films={films}
            presets={presets}
            lit={lit}
            displayPaint={displayPaint}
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
