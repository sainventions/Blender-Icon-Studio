// What the camera sees behind the icon: a subtle checkerboard (transparent backdrop), a solid colour or the worker's
// wallpaper (clear / tinted renditions). It is the scene background, so transmissive glass refracts it too.
//
// The wallpaper is defined in world units (like the worker's wallpaper plane behind the plate), so the background
// texture is framed to match the front camera (ortho_scale 2.24 / zoom on the shorter viewport side), and glass
// plates faking their transmission sample it in world (canvas) space.
import { useEffect, useLayoutEffect, useMemo } from 'react'
import { useThree } from '@react-three/fiber'
import * as THREE from 'three'
import type { FakeGlassBinding } from '../../lib/materials3d'
import { createCheckerTexture, createWallpaperTexture, WALLPAPER_EXTENT } from '../textures/procedural'

export type BackdropSpec =
  { kind: 'checker' } | { kind: 'color'; color: string } | { kind: 'wallpaper'; tone: 'light' | 'dark' }

export interface BackdropBinding {
  spec: BackdropSpec
  texture: THREE.Texture | null
  color: THREE.Color
  /** What a fake-glass surface shows through itself when nothing but the backdrop is behind it. */
  behind: FakeGlassBinding
}

const CHECKER_A = '#17171c'
const CHECKER_B = '#1e1e25'
const CHECKER_CELL = 10 // CSS px
/** Front framing (must equal CameraRig's FRONT_ORTHO_SCALE). */
const FRONT_ORTHO_SCALE = 2.24

export function backdropKey(spec: BackdropSpec): string {
  return spec.kind === 'color' ? `color:${spec.color}` : spec.kind === 'wallpaper' ? `wall:${spec.tone}` : 'checker'
}

/** Creates (and owns) the backdrop texture for `spec`. */
export function useBackdropBinding(spec: BackdropSpec): BackdropBinding {
  const key = backdropKey(spec)
  const binding = useMemo<BackdropBinding>(() => {
    if (spec.kind === 'checker') {
      const color = new THREE.Color(CHECKER_A).lerp(new THREE.Color(CHECKER_B), 0.5)
      return { spec, texture: createCheckerTexture(CHECKER_A, CHECKER_B), color, behind: { map: null, color, space: 'screen' } }
    }
    if (spec.kind === 'wallpaper') {
      const color = new THREE.Color(spec.tone === 'light' ? '#e9eafa' : '#0a1024')
      const texture = createWallpaperTexture(spec.tone)
      return { spec, texture, color, behind: { map: texture, color, space: 'canvas', extent: WALLPAPER_EXTENT } }
    }
    const color = new THREE.Color(spec.color)
    return { spec, texture: null, color, behind: { map: null, color, space: 'screen' } }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  useEffect(() => () => binding.texture?.dispose(), [binding])
  return binding
}

export function Backdrop({ binding, zoom }: { binding: BackdropBinding; zoom: number }) {
  const scene = useThree((s) => s.scene)
  const size = useThree((s) => s.size)
  const invalidate = useThree((s) => s.invalidate)

  useLayoutEffect(() => {
    scene.background = binding.texture ?? binding.color
    invalidate()
    return () => {
      if (scene.background === binding.texture || scene.background === binding.color) scene.background = null
    }
  }, [scene, binding, invalidate])

  useLayoutEffect(() => {
    const t = binding.texture
    if (!t) return
    if (binding.spec.kind === 'checker') {
      t.repeat.set(size.width / (CHECKER_CELL * 2), size.height / (CHECKER_CELL * 2))
      t.offset.set(0, 0)
    } else {
      // Show world ±half (front camera) of a texture that covers world ±WALLPAPER_EXTENT.
      const half = FRONT_ORTHO_SCALE / 2 / Math.max(0.05, zoom || 1)
      const w = Math.max(1, size.width)
      const h = Math.max(1, size.height)
      const hx = w >= h ? (half * w) / h : half
      const hy = h > w ? (half * h) / w : half
      const rx = Math.min(1, hx / WALLPAPER_EXTENT)
      const ry = Math.min(1, hy / WALLPAPER_EXTENT)
      t.repeat.set(rx, ry)
      t.offset.set(0.5 - rx / 2, 0.5 - ry / 2)
    }
    t.updateMatrix()
    invalidate()
  }, [binding, size.width, size.height, zoom, invalidate])

  return null
}
