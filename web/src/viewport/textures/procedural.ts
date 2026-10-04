// Small procedural textures generated once per session (bounded memory): film-thickness noise for iridescence,
// a sandblasted grain normal map for frosted glass and a spun (radial) anisotropy direction map. Plus the
// backdrop textures (checkerboard, wallpapers) which are owned by the Backdrop component.
import * as THREE from 'three'
import { WALLPAPERS } from '../../lib/appearance'

// ---------------------------------------------------------------------------------------------- noise
function hash2(x: number, y: number, seed: number): number {
  let h = (x * 374761393 + y * 668265263 + seed * 2147483647) | 0
  h = Math.imul(h ^ (h >>> 13), 1274126177)
  h ^= h >>> 16
  return (h >>> 0) / 4294967295
}

/** Tileable value noise with `cells` lattice cells per side, sampled at (u, v) ∈ [0,1). */
function tileNoise(u: number, v: number, cells: number, seed: number): number {
  const x = u * cells
  const y = v * cells
  const x0 = Math.floor(x)
  const y0 = Math.floor(y)
  const fx = x - x0
  const fy = y - y0
  const sx = fx * fx * (3 - 2 * fx)
  const sy = fy * fy * (3 - 2 * fy)
  const w = (i: number) => ((i % cells) + cells) % cells
  const a = hash2(w(x0), w(y0), seed)
  const b = hash2(w(x0 + 1), w(y0), seed)
  const c = hash2(w(x0), w(y0 + 1), seed)
  const d = hash2(w(x0 + 1), w(y0 + 1), seed)
  return a + (b - a) * sx + (c - a) * sy + (a - b - c + d) * sx * sy
}

function fbm(u: number, v: number, base: number, octaves: number, seed: number): number {
  let sum = 0
  let amp = 0.5
  let norm = 0
  for (let o = 0; o < octaves; o++) {
    sum += amp * tileNoise(u, v, base << o, seed + o * 17)
    norm += amp
    amp *= 0.5
  }
  return sum / norm
}

function dataTexture(
  size: number,
  fill: (u: number, v: number, out: Uint8Array, o: number) => void,
): THREE.DataTexture {
  const data = new Uint8Array(size * size * 4)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) fill(x / size, y / size, data, (y * size + x) * 4)
  }
  const t = new THREE.DataTexture(data, size, size, THREE.RGBAFormat)
  t.wrapS = THREE.RepeatWrapping
  t.wrapT = THREE.RepeatWrapping
  t.magFilter = THREE.LinearFilter
  t.minFilter = THREE.LinearMipmapLinearFilter
  t.generateMipmaps = true
  t.colorSpace = THREE.NoColorSpace
  t.needsUpdate = true
  return t
}

let filmNoise: THREE.DataTexture | null = null
/** Smooth banded noise in R (iridescence thickness map; Blender: Noise Scale ≈ 3 → MapRange to filmMin..filmMax). */
export function getFilmNoiseTexture(): THREE.DataTexture {
  if (!filmNoise) {
    filmNoise = dataTexture(256, (u, v, out, o) => {
      const n = fbm(u, v, 3, 4, 11)
      const swirl = 0.5 + 0.5 * Math.sin((n * 2.2 + u * 0.6) * Math.PI * 2)
      const val = Math.round((0.35 * n + 0.65 * swirl) * 255)
      out[o] = val
      out[o + 1] = val
      out[o + 2] = val
      out[o + 3] = 255
    })
  }
  return filmNoise
}

let grainNormal: THREE.DataTexture | null = null
/** Fine sandblast micro-bump as a tangent-space normal map (Blender: Noise Scale 400 → Bump). */
export function getGrainNormalMap(): THREE.DataTexture {
  if (!grainNormal) {
    const size = 256
    const hgt = new Float32Array(size * size)
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) hgt[y * size + x] = fbm(x / size, y / size, 32, 3, 5)
    grainNormal = dataTexture(size, (u, v, out, o) => {
      const x = Math.round(u * size)
      const y = Math.round(v * size)
      const at = (i: number, j: number) => hgt[(((j % size) + size) % size) * size + (((i % size) + size) % size)]
      const dx = (at(x + 1, y) - at(x - 1, y)) * 6
      const dy = (at(x, y + 1) - at(x, y - 1)) * 6
      const l = Math.hypot(dx, dy, 1)
      out[o] = Math.round(((-dx / l) * 0.5 + 0.5) * 255)
      out[o + 1] = Math.round(((-dy / l) * 0.5 + 0.5) * 255)
      out[o + 2] = Math.round(((1 / l) * 0.5 + 0.5) * 255)
      out[o + 3] = 255
    })
  }
  return grainNormal
}

let radialAniso: THREE.DataTexture | null = null
/**
 * Spun-metal brushing: anisotropy direction tangential to circles around the art origin (Blender: Tangent node
 * RADIAL about Z). three's anisotropyMap: RG = direction in tangent space (planar UVs → art xy), B = strength.
 */
export function getRadialAnisotropyMap(): THREE.DataTexture {
  if (!radialAniso) {
    const t = dataTexture(256, (u, v, out, o) => {
      const x = u - 0.5
      const y = v - 0.5
      const r = Math.hypot(x, y)
      const dx = r > 1e-4 ? -y / r : 1
      const dy = r > 1e-4 ? x / r : 0
      out[o] = Math.round((dx * 0.5 + 0.5) * 255)
      out[o + 1] = Math.round((dy * 0.5 + 0.5) * 255)
      out[o + 2] = Math.round(Math.min(1, r * 40) * 255)
      out[o + 3] = 255
    })
    t.wrapS = THREE.ClampToEdgeWrapping
    t.wrapT = THREE.ClampToEdgeWrapping
    t.flipY = false
    radialAniso = t
  }
  return radialAniso
}

// ---------------------------------------------------------------------------------------------- backdrops
export function createCheckerTexture(a: string, b: string): THREE.CanvasTexture {
  const c = document.createElement('canvas')
  c.width = 2
  c.height = 2
  const ctx = c.getContext('2d')!
  ctx.fillStyle = a
  ctx.fillRect(0, 0, 2, 2)
  ctx.fillStyle = b
  ctx.fillRect(1, 0, 1, 1)
  ctx.fillRect(0, 1, 1, 1)
  const t = new THREE.CanvasTexture(c)
  t.colorSpace = THREE.SRGBColorSpace
  t.magFilter = THREE.NearestFilter
  t.minFilter = THREE.NearestFilter
  t.generateMipmaps = false
  t.wrapS = THREE.RepeatWrapping
  t.wrapT = THREE.RepeatWrapping
  return t
}

/** Half-size (world units) of the square the wallpaper texture covers, centred on the icon. */
export const WALLPAPER_EXTENT = 3

function srgbToLinear(c: number): number {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}
function linearToSrgb8(c: number): number {
  const v = c <= 0.0031308 ? c * 12.92 : 1.055 * Math.max(0, c) ** (1 / 2.4) - 0.055
  return Math.round(Math.max(0, Math.min(1, v)) * 255)
}
function hexLinear(hex: string): [number, number, number] {
  const n = parseInt(hex.replace('#', '').slice(0, 6), 16) || 0
  return [srgbToLinear(((n >> 16) & 255) / 255), srgbToLinear(((n >> 8) & 255) / 255), srgbToLinear((n & 255) / 255)]
}

/**
 * The worker's wallpaper (scene.py `_wallpaper`) evaluated per pixel in linear light over world x, y ∈ ±extent:
 * vertical gradient top → bottom over y = +2.2 … −2.2, then each blob mixed in by 0.85 × smoothstep falloff from
 * its centre (x, y) × 1.6 out to r × 1.9. Texture v = 1 is world y = +extent.
 */
export function createWallpaperTexture(kind: 'light' | 'dark', extent = WALLPAPER_EXTENT, size = 384): THREE.DataTexture {
  const spec = WALLPAPERS[kind]
  const top = hexLinear(spec.top)
  const bottom = hexLinear(spec.bottom)
  const mix = 0.85
  const blobs = spec.blobs.map((b) => ({ x: b.x * 1.6, y: b.y * 1.6, r: b.r * 1.9, c: hexLinear(b.color) }))
  const data = new Uint8Array(size * size * 4)
  const col = [0, 0, 0]
  for (let j = 0; j < size; j++) {
    // DataTexture rows run bottom → top (flipY is not applied to data textures).
    const y = ((j + 0.5) / size) * 2 * extent - extent
    const t = Math.max(0, Math.min(1, (2.2 - y) / 4.4))
    for (let i = 0; i < size; i++) {
      const x = ((i + 0.5) / size) * 2 * extent - extent
      for (let k = 0; k < 3; k++) col[k] = top[k] + (bottom[k] - top[k]) * t
      for (const b of blobs) {
        const d = Math.hypot(x - b.x, y - b.y)
        const u = Math.max(0, Math.min(1, d / b.r))
        const f = (1 - u * u * (3 - 2 * u)) * mix
        for (let k = 0; k < 3; k++) col[k] += (b.c[k] - col[k]) * f
      }
      const o = (j * size + i) * 4
      data[o] = linearToSrgb8(col[0])
      data[o + 1] = linearToSrgb8(col[1])
      data[o + 2] = linearToSrgb8(col[2])
      data[o + 3] = 255
    }
  }
  const tex = new THREE.DataTexture(data, size, size, THREE.RGBAFormat)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.wrapS = THREE.ClampToEdgeWrapping
  tex.wrapT = THREE.ClampToEdgeWrapping
  tex.magFilter = THREE.LinearFilter
  tex.minFilter = THREE.LinearMipmapLinearFilter
  tex.generateMipmaps = true
  tex.needsUpdate = true
  return tex
}
