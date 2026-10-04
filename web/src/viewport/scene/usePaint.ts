// Resolve a Fill (+ the layer texture URL) into a PaintBinding (lib/materials3d): texture map or flat colour, like the
// worker's scene.paint_spec ('auto' = the layer's rasterised art; overrides = solid / gradient; 'none' = not drawn).
import { useMemo } from 'react'
import * as THREE from 'three'
import type { Fill } from '../../types'
import type { PaintBinding } from '../../lib/materials3d'
import { fillPreviewColor, paintSource, useGradientAsset } from '../textures/fillTextures'
import { useTextureAsset } from '../textures/layerTextures'

export interface ResolvedPaint {
  binding: PaintBinding
  /** Fill 'none' (plate: not drawn). */
  none: boolean
  /** Final paint is available (texture loaded or not needed). */
  ready: boolean
}

/** Gradient fills with translucent stops carry alpha (worker paint_spec has_alpha). */
function gradientAlpha(fill: Fill): boolean {
  return (fill.type === 'linear' || fill.type === 'radial') && fill.stops.some((s) => (s.opacity ?? 1) < 0.999)
}

export function usePaint(fill: Fill, textureUrl: string | null, fallbackHex: string): ResolvedPaint {
  const spec = useMemo(() => paintSource(fill, textureUrl, fallbackHex), [fill, textureUrl, fallbackHex])
  const tex = useTextureAsset(spec.kind === 'texture' ? spec.url : null)
  const grad = useGradientAsset(spec)
  const texReady = !!tex?.ready
  const texStats = tex?.stats ?? null

  return useMemo<ResolvedPaint>(() => {
    if (spec.kind === 'none') return { binding: { map: null, color: new THREE.Color(1, 1, 1) }, none: true, ready: true }
    if (spec.kind === 'color')
      return { binding: { map: null, color: new THREE.Color().setStyle(spec.color) }, none: false, ready: true }
    if (spec.kind === 'gradient' && grad) {
      return {
        binding: { map: grad.texture, color: new THREE.Color(...grad.stats.avg), alpha: gradientAlpha(spec.fill) },
        none: false,
        ready: true,
      }
    }
    if (spec.kind === 'texture' && tex && texReady) {
      const color = texStats ? new THREE.Color(...texStats.avg) : new THREE.Color().setStyle(fallbackHex)
      return { binding: { map: tex.texture, color }, none: false, ready: true }
    }
    // Loading (or failed): flat preview colour.
    return { binding: { map: null, color: new THREE.Color().setStyle(fallbackHex) }, none: false, ready: !!tex?.failed }
  }, [spec, grad, tex, texReady, texStats, fallbackHex])
}

export { fillPreviewColor }
