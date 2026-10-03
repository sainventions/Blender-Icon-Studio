// Layer art textures (server-rasterised RGBA PNGs covering the art square: u=(x+1)/2, v=(y+1)/2) and raster card
// images. Loaded once per URL, shared, reference counted, disposed when unused.
import { useEffect, useReducer } from 'react'
import * as THREE from 'three'
import { RefCache, useCached } from '../refCache'

export interface PaintStats {
  /** Linear-light luminance range over opaque pixels (mono/tint renditions stretch it to MONO_FLOOR..1). */
  lumMin: number
  lumMax: number
  /** Average linear colour over opaque pixels (fallback paint). */
  avg: [number, number, number]
  /** Largest alpha (0..1) of the downsampled image: < 0.5 = a translucent overlay (shading / highlight art). */
  alphaMax: number
}

export interface TextureAsset {
  texture: THREE.Texture
  ready: boolean
  failed: boolean
  stats: PaintStats | null
  listeners: Set<() => void>
}

const loader = new THREE.TextureLoader()

function srgbToLinear(c: number): number {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

export function imageStats(image: CanvasImageSource, size = 64): PaintStats | null {
  try {
    const canvas = document.createElement('canvas')
    canvas.width = size
    canvas.height = size
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    if (!ctx) return null
    ctx.drawImage(image, 0, 0, size, size)
    const data = ctx.getImageData(0, 0, size, size).data
    let alphaMax = 0
    for (let i = 3; i < data.length; i += 4) if (data[i] > alphaMax) alphaMax = data[i]
    // Colour statistics over the opaque pixels — or, for a translucent overlay, over everything visible.
    const cut = alphaMax >= 128 ? 128 : 8
    let lo = Infinity
    let hi = -Infinity
    let r = 0
    let g = 0
    let b = 0
    let count = 0
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < cut) continue
      const lr = srgbToLinear(data[i] / 255)
      const lg = srgbToLinear(data[i + 1] / 255)
      const lb = srgbToLinear(data[i + 2] / 255)
      const l = 0.2126 * lr + 0.7152 * lg + 0.0722 * lb
      if (l < lo) lo = l
      if (l > hi) hi = l
      r += lr
      g += lg
      b += lb
      count++
    }
    if (!count) return null
    return { lumMin: lo, lumMax: hi, avg: [r / count, g / count, b / count], alphaMax: alphaMax / 255 }
  } catch {
    return null // tainted canvas etc.
  }
}

function loadAsset(url: string): TextureAsset {
  const listeners = new Set<() => void>()
  const notify = () => listeners.forEach((fn) => fn())
  const texture = loader.load(
    url,
    (tex) => {
      asset.stats = imageStats(tex.image as CanvasImageSource)
      asset.ready = true
      notify()
    },
    undefined,
    () => {
      asset.failed = true
      notify()
    },
  )
  const asset: TextureAsset = { texture, ready: false, failed: false, stats: null, listeners }
  const t = texture
  t.colorSpace = THREE.SRGBColorSpace
  t.anisotropy = 8
  t.wrapS = THREE.ClampToEdgeWrapping
  t.wrapT = THREE.ClampToEdgeWrapping
  t.minFilter = THREE.LinearMipmapLinearFilter
  t.magFilter = THREE.LinearFilter
  t.generateMipmaps = true
  return asset
}

export const textureCache = new RefCache<string, TextureAsset>((a) => {
  a.listeners.clear()
  a.texture.dispose()
}, 2500)

/** Shared texture for `url` (null while unknown). Re-renders the caller when it finishes loading. */
export function useTextureAsset(url: string | null): TextureAsset | null {
  const asset = useCached(textureCache, url, () => loadAsset(url as string))
  const [, force] = useReducer((c: number) => c + 1, 0)
  const renderedSettled = !!asset && (asset.ready || asset.failed)
  useEffect(() => {
    if (!asset) return
    const settled = asset.ready || asset.failed
    if (settled !== renderedSettled) {
      force() // finished loading between render and commit
      return
    }
    if (settled) return
    const fn = () => force()
    asset.listeners.add(fn)
    return () => {
      asset.listeners.delete(fn)
    }
  }, [asset, renderedSettled])
  return asset
}
