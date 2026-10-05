// Fill overrides (solid / linear / radial / system-*) → paint sources. Gradients are drawn into CanvasTextures in
// the −1..1 square of their space (ART space for layer fills, CANVAS space for the plate) with the same mapping
// as the layer textures (u=(x+1)/2, v=(y+1)/2). Canvas 2D interpolates stops in sRGB, like SVG and the worker.
import * as THREE from 'three'
import type { Fill, FillLinear, FillRadial, FillSystem, GradientStop } from '../../types'
import { RefCache, useCached } from '../refCache'
import type { PaintStats } from './layerTextures'

/** PLAN §2 system fills: linear top → bottom. */
export const SYSTEM_FILLS = {
  'system-light': ['#ffffff', '#e4e5ea'],
  'system-dark': ['#3a3a3f', '#111114'],
} as const

export type GradientFill = FillLinear | FillRadial | FillSystem

function isSystemFill(f: GradientFill): f is FillSystem {
  return f.type === 'system-light' || f.type === 'system-dark'
}

export type PaintSourceSpec =
  | { kind: 'color'; color: string; opacity: number }
  | { kind: 'gradient'; key: string; fill: GradientFill }
  | { kind: 'texture'; url: string }
  | { kind: 'none' }

/** What paints a surface: the layer texture for 'auto', otherwise the override. */
export function paintSource(fill: Fill, textureUrl: string | null, fallback: string): PaintSourceSpec {
  switch (fill.type) {
    case 'auto':
      return textureUrl ? { kind: 'texture', url: textureUrl } : { kind: 'color', color: fallback, opacity: 1 }
    case 'none':
      return { kind: 'none' }
    case 'solid':
      return { kind: 'color', color: fill.color, opacity: fill.opacity ?? 1 }
    case 'linear':
    case 'radial':
    case 'system-light':
    case 'system-dark':
      return { kind: 'gradient', key: JSON.stringify(fill), fill }
  }
}

export interface GradientAsset {
  texture: THREE.CanvasTexture
  stats: PaintStats
}

const SIZE = 512

const _rgb = { r: 0, g: 0, b: 0 }
/**
 * CSS colour for the (sRGB) 2D canvas. NB: THREE.Color stores *linear* working-space values, so the sRGB components
 * must be read back with getRGB(…, SRGBColorSpace); writing c.r/g/b directly would decode the colour twice.
 */
function rgba(hex: string, opacity: number): string {
  new THREE.Color().setStyle(hex).getRGB(_rgb, THREE.SRGBColorSpace)
  const [r, g, b] = [_rgb.r, _rgb.g, _rgb.b].map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255))
  const a = Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : 1
  return `rgba(${r},${g},${b},${a})`
}

function sortedStops(stops: GradientStop[]): GradientStop[] {
  const s = [...stops].sort((a, b) => a.offset - b.offset)
  return s.length ? s : [{ offset: 0, color: '#ffffff', opacity: 1 }]
}

function addStops(g: CanvasGradient, stops: GradientStop[]): void {
  for (const s of sortedStops(stops)) g.addColorStop(Math.max(0, Math.min(1, s.offset)), rgba(s.color, s.opacity ?? 1))
}

function statsFromColors(colors: string[]): PaintStats {
  const avg: [number, number, number] = [0, 0, 0]
  for (const hex of colors) {
    const c = new THREE.Color().setStyle(hex) // → linear working space
    avg[0] += c.r / colors.length
    avg[1] += c.g / colors.length
    avg[2] += c.b / colors.length
  }
  return { avg }
}

function drawGradient(fill: GradientFill): GradientAsset {
  const canvas = document.createElement('canvas')
  canvas.width = SIZE
  canvas.height = SIZE
  const ctx = canvas.getContext('2d')!
  const h = SIZE / 2
  // art/canvas (x, y up) → pixel (y down)
  const px = (x: number) => (x + 1) * h
  const py = (y: number) => (1 - y) * h
  let colors: string[]
  if (isSystemFill(fill)) {
    const [top, bottom] = SYSTEM_FILLS[fill.type]
    const g = ctx.createLinearGradient(0, 0, 0, SIZE)
    g.addColorStop(0, top)
    g.addColorStop(1, bottom)
    ctx.fillStyle = g
    ctx.fillRect(0, 0, SIZE, SIZE)
    colors = [top, bottom]
  } else if (fill.type === 'linear') {
    const g = ctx.createLinearGradient(px(fill.start[0]), py(fill.start[1]), px(fill.end[0]), py(fill.end[1]))
    addStops(g, fill.stops)
    ctx.fillStyle = g
    ctx.fillRect(0, 0, SIZE, SIZE)
    colors = fill.stops.map((s) => s.color)
  } else {
    const m = fill.matrix ?? [1, 0, 0, 1, 0, 0]
    // pixel ← art: [h, 0, 0, −h, h, h]; art ← gradient: m. Combined = A·M.
    const [a, b, c, d, e, f] = m
    const A = [h * a, -h * b, h * c, -h * d, h * e + h, -h * f + h] as const
    ctx.setTransform(A[0], A[1], A[2], A[3], A[4], A[5])
    const [cx, cy] = fill.center
    const [fx, fy] = fill.focal ?? fill.center
    const g = ctx.createRadialGradient(fx, fy, 0, cx, cy, Math.max(1e-6, fill.radius))
    addStops(g, fill.stops)
    ctx.fillStyle = g
    // Cover the whole canvas: inverse-map its corners into gradient space.
    const inv = new DOMMatrix([A[0], A[1], A[2], A[3], A[4], A[5]]).inverse()
    const corners = [
      [0, 0],
      [SIZE, 0],
      [0, SIZE],
      [SIZE, SIZE],
    ].map(([x, y]) => inv.transformPoint({ x, y }))
    const xs = corners.map((p) => p.x)
    const ys = corners.map((p) => p.y)
    const minX = Math.min(...xs)
    const minY = Math.min(...ys)
    ctx.fillRect(minX, minY, Math.max(...xs) - minX, Math.max(...ys) - minY)
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    colors = fill.stops.map((s: GradientStop) => s.color)
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.wrapS = THREE.ClampToEdgeWrapping
  texture.wrapT = THREE.ClampToEdgeWrapping
  texture.anisotropy = 4
  return { texture, stats: statsFromColors(colors.length ? colors : ['#ffffff']) }
}

export const gradientCache = new RefCache<string, GradientAsset>((a) => a.texture.dispose())

export function useGradientAsset(spec: PaintSourceSpec | null): GradientAsset | null {
  const isGrad = spec?.kind === 'gradient'
  return useCached(gradientCache, isGrad ? spec.key : null, () => drawGradient((spec as { fill: GradientFill }).fill))
}

/** Representative flat colour of a fill (sRGB hex), used before textures load and for luminance stats. */
export function fillPreviewColor(fill: Fill, fallback = '#ffffff'): string {
  switch (fill.type) {
    case 'solid':
      return fill.color
    case 'linear':
    case 'radial': {
      const s = sortedStops(fill.stops)
      return s[Math.floor(s.length / 2)]?.color ?? fallback
    }
    case 'system-light':
      return '#f2f2f5'
    case 'system-dark':
      return '#26262a'
    default:
      return fallback
  }
}
