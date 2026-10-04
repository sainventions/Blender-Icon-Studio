// The icon plate (worker scene._plate): a height-field body over the parametric outline (squircle / circle / rounded /
// square) with a round edge, front face at z = 0, back at −thickness (PLAN §3), ONE Principled material from
// plate.material painted by the plate fill (canvas space). A glass plate covered by glass layers is drawn opaque with
// what lies behind it (three.js has no transmission through transmission).
import { memo, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { Canvas as CanvasSpec, Presets } from '../../types'
import { applyPrincipled, BLENDED_RENDER_ORDER, isTransmissive, PrincipledMaterial, resolveMaterial } from '../../lib/materials3d'
import { useCached } from '../refCache'
import { bodyCache, buildPlateBody, plateGeometryKey, plateParams } from '../geometry/layerGeometry'
import type { ResolvedPaint } from './usePaint'

interface Props {
  canvas: CanvasSpec
  presets: Presets | null
  paint: ResolvedPaint
  /** Glass layers above a glass plate: draw it opaque × this colour (what lies behind it). Null = real transmission. */
  covered: THREE.Color | null
}

export const Plate = memo(function Plate({ canvas, presets, paint, covered }: Props) {
  const invalidate = useThree((s) => s.invalidate)
  const params = plateParams(canvas.shape, canvas.cornerRadius, canvas.plate)
  const key = plateGeometryKey(params)
  const body = useCached(bodyCache, key, () => buildPlateBody(params))
  const material = useMemo(() => new PrincipledMaterial(), [])
  const meshRef = useRef<THREE.Mesh>(null)
  useEffect(() => () => material.dispose(), [material])
  const principled = useMemo(() => resolveMaterial(canvas.plate.material, null, presets).params, [canvas.plate.material, presets])

  useLayoutEffect(() => {
    applyPrincipled(material, principled, {
      paint: paint.binding,
      opacity: 1,
      thickness: Math.max(1e-4, canvas.plate.thickness),
      covered,
    })
    if (meshRef.current) meshRef.current.renderOrder = material.routed ? BLENDED_RENDER_ORDER.plate : 0
    invalidate()
  }, [material, principled, paint, covered, canvas.plate.thickness, invalidate])

  if (!canvas.plate.visible || canvas.shape === 'none' || paint.none || !body) return null
  return (
    <mesh
      ref={meshRef}
      name="plate"
      geometry={body.geometry}
      material={material}
      position-z={-Math.max(0, canvas.plate.thickness) / 2}
      // a glass plate mostly shows what lies behind it: the key's shadow on it is faint in Cycles
      receiveShadow={!isTransmissive(principled)}
      castShadow={false}
      renderOrder={material.routed ? BLENDED_RENDER_ORDER.plate : 0}
    />
  )
})
