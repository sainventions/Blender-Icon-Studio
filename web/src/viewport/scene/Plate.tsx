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
} from '../../lib/materials3d'
import { useCached } from '../refCache'
import { buildPlateGeometry, geometryCache, plateGeometryKey, plateParams } from '../geometry/layerGeometry'
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
}

export const Plate = memo(function Plate({ canvas, presets, paint, behind, rimDir, lit }: Props) {
  const invalidate = useThree((s) => s.invalidate)
  const params = plateParams(canvas.shape, canvas.cornerRadius, canvas.plate)
  const key = plateGeometryKey(params)
  const geometry = useCached(geometryCache, key, () => buildPlateGeometry(params))
  const material = useMemo(() => new IconMaterial(), [])
  const meshRef = useRef<THREE.Mesh>(null)
  const blended = useRef(false)
  useEffect(() => () => material.dispose(), [material])

  const spec = useMemo(() => describeMaterial(canvas.plate.material, presets), [canvas.plate.material, presets])

  useLayoutEffect(() => {
    applyIconMaterial(material, spec, {
      paint: paint.binding,
      thickness: canvas.plate.thickness,
      // The plate is always the bottom-most surface: a glass plate is faked over the backdrop.
      fake: behind,
      rimDir,
      opacity: paint.opacity,
      lit,
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
  }, [material, spec, paint, behind, rimDir, lit, canvas.plate.thickness, invalidate])

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
