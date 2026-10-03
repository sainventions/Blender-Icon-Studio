// Resolve a Fill (+ the layer texture URL) into a PaintBinding for IconMaterial: texture map or flat colour, plus the
// luminance range the mono/tint renditions stretch.
import { useMemo } from 'react'
import * as THREE from 'three'
import type { Fill } from '../../types'
import type { PaintBinding } from '../../lib/materials3d'
import { colorStats, fillPreviewColor, paintSource, useGradientAsset } from '../textures/fillTextures'
import { useTextureAsset } from '../textures/layerTextures'

export interface ResolvedPaint {
  binding: PaintBinding
  /** Solid-fill opacity multiplier. */
  opacity: number
  /** Fill 'none' (plate: not drawn). */
  none: boolean
  /** Final paint is available (texture loaded or not needed). */
  ready: boolean
}

export function usePaint(fill: Fill, textureUrl: string | null, fallbackHex: string): ResolvedPaint {
  const spec = useMemo(() => paintSource(fill, textureUrl, fallbackHex), [fill, textureUrl, fallbackHex])
  const tex = useTextureAsset(spec.kind === 'texture' ? spec.url : null)
  const grad = useGradientAsset(spec)
  const texReady = !!tex?.ready
  const texStats = tex?.stats ?? null

  return useMemo<ResolvedPaint>(() => {
    if (spec.kind === 'none') {
      return {
        binding: { map: null, color: new THREE.Color(1, 1, 1), lumRange: [1, 1] },
        opacity: 1,
        none: true,
        ready: true,
      }
    }
    if (spec.kind === 'color') {
      const st = colorStats(spec.color)
      return {
        binding: { map: null, color: new THREE.Color().setStyle(spec.color), lumRange: [st.lumMin, st.lumMax] },
        opacity: spec.opacity,
        none: false,
        ready: true,
      }
    }
    if (spec.kind === 'gradient' && grad) {
      const st = grad.stats
      return {
        binding: { map: grad.texture, color: new THREE.Color(...st.avg), lumRange: [st.lumMin, st.lumMax] },
        opacity: 1,
        none: false,
        ready: true,
      }
    }
    if (spec.kind === 'texture' && tex && texReady) {
      const st = texStats
      const color = st ? new THREE.Color(...st.avg) : new THREE.Color().setStyle(fallbackHex)
      return {
        binding: {
          map: tex.texture,
          color,
          lumRange: st ? [st.lumMin, st.lumMax] : [0, 1],
          alphaMax: st ? st.alphaMax : 1,
        },
        opacity: 1,
        none: false,
        ready: true,
      }
    }
    // Loading (or failed): flat preview colour.
    const st = colorStats(fallbackHex)
    return {
      binding: { map: null, color: new THREE.Color().setStyle(fallbackHex), lumRange: [st.lumMin, st.lumMax] },
      opacity: 1,
      none: false,
      ready: !!tex?.failed,
    }
  }, [spec, grad, tex, texReady, texStats, fallbackHex])
}

export { fillPreviewColor }
