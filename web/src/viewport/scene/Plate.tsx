// The icon plate: parametric outline (squircle / circle / rounded / square) extruded with a round bevel, front face at
// z = 0, back face at −thickness (PLAN §3). Its fill is drawn in canvas space; glass plate materials (clear
// renditions) render as fake glass over the backdrop so the layers above can still refract them.
import { memo, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { Canvas as CanvasSpec, Presets } from '../../types'
import {
  applyIconMaterial,
  blendInOpaquePass,
  BLENDED_RENDER_ORDER,
  describeMaterial,
  IconMaterial,
  type FakeGlassBinding,
  type PaintTransform,
} from '../../lib/materials3d'
import { useCached } from '../refCache'
import { buildPlateGeometry, geometryCache, plateGeometryKey, plateParams } from '../geometry/layerGeometry'
import { createWallpaperTexture, WALLPAPER_EXTENT } from '../textures/procedural'
import type { ResolvedPaint } from './usePaint'

interface Props {
  canvas: CanvasSpec
  presets: Presets | null
  paint: ResolvedPaint
  /** Backdrop seen through a glass plate. */
  behind: FakeGlassBinding
  rimDir: THREE.Vector3
  /** Liquid Glass self-illumination (worker `lit`). */
  lit: number
  /** Paint pre-compensation for the colour mode's view transform (worker display_paint). */
  displayPaint: PaintTransform
}

export const Plate = memo(function Plate({ canvas, presets, paint, behind, rimDir, lit, displayPaint }: Props) {
  const invalidate = useThree((s) => s.invalidate)
  const params = plateParams(canvas.shape, canvas.cornerRadius, canvas.plate)
  const key = plateGeometryKey(params)
  const geometry = useCached(geometryCache, key, () => buildPlateGeometry(params))
  const material = useMemo(() => new IconMaterial(), [])
  const meshRef = useRef<THREE.Mesh>(null)
  const blended = useRef(false)
  useEffect(() => () => material.dispose(), [material])

  const spec = useMemo(() => describeMaterial(canvas.plate.material, presets), [canvas.plate.material, presets])
  // Over a rendition wallpaper a glass plate uses the worker's backdrop-glass model (frosted pane, see FakeGlassBinding):
  // the wallpaper as seen through the frost (blobs spread out), × glass colour, screened by the frost's scatter.
  const frost = spec.transmission > 0 ? Math.round(spec.roughness * 100) / 100 : 0
  const frosted = useMemo(
    () => (behind.tone && behind.space === 'canvas' ? createWallpaperTexture(behind.tone, behind.extent ?? WALLPAPER_EXTENT, 256, frost) : null),
    [behind.tone, behind.space, behind.extent, frost],
  )
  useEffect(() => () => frosted?.dispose(), [frosted])
  const fake = useMemo<FakeGlassBinding>(
    () => (behind.tone && frosted ? { ...behind, map: frosted, backdropGlass: true } : behind),
    [behind, frosted],
  )

  useLayoutEffect(() => {
    applyIconMaterial(material, spec, {
      paint: paint.binding,
      thickness: canvas.plate.thickness,
      // The plate is always the bottom-most surface: a glass plate is faked over the backdrop.
      fake,
      rimDir,
      opacity: paint.opacity,
      lit,
      displayPaint,
      plate: true, // worker spec 'plate': no white-ice / white-milk body
    })
    // A semi-transparent plate fill must stay in the opaque pass, or glass layers would neither show nor refract it.
    material.blending = THREE.NormalBlending // undo an earlier routing (fill opacity back to 1)
    const routed = blendInOpaquePass(material)
    if (routed !== blended.current) {
      blended.current = routed
      material.needsUpdate = true
    }
    if (meshRef.current) meshRef.current.renderOrder = routed ? BLENDED_RENDER_ORDER.plate : 0
    invalidate()
  }, [material, spec, paint, fake, rimDir, lit, displayPaint, canvas.plate.thickness, invalidate])

  if (!canvas.plate.visible || canvas.shape === 'none' || paint.none || !geometry) return null
  return (
    <mesh
      ref={meshRef}
      name="plate"
      geometry={geometry}
      material={material}
      receiveShadow
      castShadow={false}
      renderOrder={blended.current ? BLENDED_RENDER_ORDER.plate : 0}
    />
  )
})
