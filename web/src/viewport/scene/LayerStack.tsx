// The layer stack: one group per visible layer (canvas.art ∘ layer.transform, z from depth.z × camera.explode plus
// the animated UI explode spread), one pill mesh per region ('individual') or the union silhouette ('combined'),
// raster cards for images without vector regions, picking / hover / drag-to-move.
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import * as THREE from 'three'
import type {
  ArtTransform,
  BlendMode,
  Layer,
  LayerGeometry,
  LayerTransform,
  MaterialSpec,
  Presets,
  RasterCard,
} from '../../types'
import {
  applyIconMaterial,
  blendInOpaquePass,
  BLENDED_RENDER_ORDER,
  describeMaterial,
  IconMaterial,
  type FakeGlassBinding,
  type IconMaterialSpec,
  type MaterialContext,
  type PaintUv,
} from '../../lib/materials3d'
import { useCached } from '../refCache'
import { effectiveDepth, geometryCache, layerBodyParts, layerScale, type BodyPart } from '../geometry/layerGeometry'
import { fillPreviewColor } from '../textures/fillTextures'
import { useTextureAsset } from '../textures/layerTextures'
import { EXPLODE_SPREAD, useViewportStore } from './store'
import { usePaint } from './usePaint'

export interface StackEntry {
  layer: Layer
  lg: LayerGeometry
  /** Stack level (index in project.layers, bottom = 0). */
  level: number
  baseZ: number
  /** World thickness (= layer.depth.thickness). */
  thickness: number
  /** Uniform local → world scale S = art.scale × transform.scale. */
  scale: number
  spec: IconMaterialSpec
  fake: boolean
  /**
   * Blended (semi-transparent / blend-mode) bodies of this layer are drawn in the opaque pass (blendInOpaquePass) so
   * refracting glass above them shows and refracts them. False only when refracting glass lies below the layer and
   * none above it: then they stay in the transparent pass, drawn after the glass, or they would punch a hole in it.
   */
  routeBlended: boolean
  /** Canvas-space bbox (after transforms). */
  bbox: [number, number, number, number]
}

/** Icon Composer "Effects" off (`glass: false`): the worker renders the layer with the unlit `flat` preset. */
const FLAT_MATERIAL: MaterialSpec = { preset: 'flat', params: {} }

/** The material a layer actually renders with. */
export function layerMaterial(layer: Layer): MaterialSpec {
  return layer.glass === false ? FLAT_MATERIAL : layer.material
}

const specMemo = new WeakMap<object, { presets: Presets | null; spec: IconMaterialSpec }>()
function specFor(material: MaterialSpec, presets: Presets | null): IconMaterialSpec {
  const hit = specMemo.get(material)
  if (hit && hit.presets === presets) return hit.spec
  const spec = describeMaterial(material, presets)
  specMemo.set(material, { presets, spec })
  return spec
}

/** Layer back face above the plate's front face (worker LAYER_EPS). */
const LAYER_EPS = 0.002
/** Raster cards: the front face of the worker's 0.004-thick card centred at 0.002 + 0.0005 (world units). */
const CARD_Z = 0.0045

/** Visible layers with geometry, material specs and the D7 fake-glass assignment. */
export function buildStack(
  layers: Layer[],
  geometry: Record<string, LayerGeometry> | null | undefined,
  art: ArtTransform,
  camExplode: number,
  presets: Presets | null,
): StackEntry[] {
  const out: StackEntry[] = []
  layers.forEach((layer, level) => {
    const lg = geometry?.[layer.id]
    if (!layer.visible || !lg || layer.opacity <= 0.001) return
    const t = layer.transform
    const s = layerScale(art.scale, t.scale)
    const tx = art.x * t.scale + t.x
    const ty = art.y * t.scale + t.y
    const [x0, y0, x1, y1] = lg.bbox ?? [-1, -1, 1, 1]
    const bx: [number, number, number, number] = [
      Math.min(x0 * s, x1 * s) + tx,
      Math.min(y0 * s, y1 * s) + ty,
      Math.max(x0 * s, x1 * s) + tx,
      Math.max(y0 * s, y1 * s) + ty,
    ]
    out.push({
      layer,
      lg,
      level,
      baseZ: (Number.isFinite(layer.depth.z) ? layer.depth.z : 0) * camExplode + LAYER_EPS,
      thickness: Math.max(1e-4, layer.depth.thickness),
      scale: s,
      spec: specFor(layerMaterial(layer), presets),
      fake: false,
      routeBlended: true,
      bbox: bx,
    })
  })
  // PLAN D7: three.js (like EEVEE) cannot show transmissive glass through transmissive glass. Glass covered by
  // another glass layer is rendered as opaque fake glass so the glass above can still refract it.
  const overlaps = (a: StackEntry['bbox'], b: StackEntry['bbox']) =>
    a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
  for (let i = 0; i < out.length; i++) {
    if (out[i].spec.transmission <= 0) continue
    for (let j = i + 1; j < out.length; j++) {
      if (out[j].spec.transmission > 0 && overlaps(out[i].bbox, out[j].bbox)) {
        out[i].fake = true
        break
      }
    }
  }
  // Pass routing of blended bodies (see StackEntry.routeBlended): refracting glass = transmissive and not fake.
  const refracts = (e: StackEntry) => e.spec.transmission > 0 && !e.fake
  for (let i = 0; i < out.length; i++) {
    let above = false
    let below = false
    for (let j = 0; j < out.length && !above; j++) {
      if (j === i || !refracts(out[j]) || !overlaps(out[i].bbox, out[j].bbox)) continue
      if (j > i) above = true
      else below = true
    }
    out[i].routeBlended = above || !below
  }
  return out
}

/** The plate as framing geometry: its outline (canvas space) extruded over [−thickness, 0]. */
export interface PlateFrame {
  outline: [number, number][]
  thickness: number
}

/**
 * World-space points whose hull bounds what the camera must show at a given explode amount: the plate outline at its
 * front and back faces (tighter than its bounding square for rounded shapes) and the 8 corners of every layer box.
 * Written into `out` (xyz triplets, reused between frames); returns the number of points.
 */
export function stackFramePoints(
  stack: StackEntry[],
  explode: number,
  plate: PlateFrame | null,
  out: number[],
): number {
  let n = 0
  const push = (x: number, y: number, z: number) => {
    out[n * 3] = x
    out[n * 3 + 1] = y
    out[n * 3 + 2] = z
    n++
  }
  if (plate) {
    for (const [x, y] of plate.outline) {
      push(x, y, 0)
      push(x, y, -plate.thickness)
    }
  }
  for (const e of stack) {
    const z0 = e.baseZ + explode * EXPLODE_SPREAD * (e.level + 1)
    const z1 = z0 + e.thickness
    const [x0, y0, x1, y1] = e.bbox
    for (let k = 0; k < 2; k++) {
      const z = k ? z1 : z0
      push(x0, y0, z)
      push(x1, y0, z)
      push(x1, y1, z)
      push(x0, y1, z)
    }
  }
  out.length = n * 3
  return n
}

function linLum(hex: string): number {
  const c = new THREE.Color().setStyle(hex)
  return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b
}

/**
 * Icon-wide luminance range of the art (region paints / fill overrides) — the mono and tint renditions stretch
 * luminance against it so the brightest region of the whole icon becomes white (PLAN §5).
 */
export function iconLumRange(stack: StackEntry[]): [number, number] {
  let lo = Infinity
  let hi = -Infinity
  const add = (hex: string) => {
    const l = linLum(hex)
    if (l < lo) lo = l
    if (l > hi) hi = l
  }
  for (const { layer, lg } of stack) {
    const f = layer.fill
    if (f.type === 'solid') add(f.color)
    else if (f.type === 'linear' || f.type === 'radial') f.stops.forEach((st) => add(st.color))
    else if (f.type === 'system-light') ['#ffffff', '#e4e5ea'].forEach(add)
    else if (f.type === 'system-dark') ['#3a3a3f', '#111114'].forEach(add)
    else if (f.type === 'auto') {
      for (const r of lg.regions) {
        const p = r.paint
        if (p.type === 'solid') add(p.color)
        else if (p.type === 'linear' || p.type === 'radial') p.stops.forEach((st) => add(st.color))
      }
    }
  }
  return Number.isFinite(lo) ? [lo, hi] : [0, 1]
}

export function stackTop(stack: StackEntry[], explode: number): number {
  let top = 0
  for (const e of stack) top = Math.max(top, e.baseZ + explode * EXPLODE_SPREAD * (e.level + 1) + e.thickness)
  return top
}

interface LayerBodyProps {
  entry: StackEntry
  art: ArtTransform
  plateBehind: FakeGlassBinding
  rimDir: THREE.Vector3
  rimColor: THREE.Color
  view: 'front' | 'orbit'
  draggable: boolean
  onSelect: (id: string | null) => void
  onTransform: (id: string, t: LayerTransform) => void
  /** Icon-wide luminance range (mono / tint renditions). */
  intentLum: [number, number]
  presets: Presets | null
  /** Liquid Glass self-illumination (worker `lit`). */
  lit: number
}

export const LayerBody = memo(function LayerBody(p: LayerBodyProps) {
  const { entry, art } = p
  const { layer, lg } = entry
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  const gl = useThree((s) => s.gl)
  const groupRef = useRef<THREE.Group>(null)

  const depth = useMemo(() => effectiveDepth(layer, lg, entry.scale), [layer, lg, entry.scale])
  const parts = useMemo(() => layerBodyParts(layer, lg, depth), [layer, lg, depth])
  const fallback = useMemo(() => {
    const r = lg.regions?.[0]
    return fillPreviewColor(layer.fill.type === 'auto' && r ? r.paint : layer.fill, '#c8ccd6')
  }, [layer.fill, lg])
  const paint = usePaint(layer.fill, lg.texture || null, fallback)

  const ctx = useMemo<MaterialContext>(() => {
    const [x0, y0, x1, y1] = lg.bbox ?? [-1, -1, 1, 1]
    const intent = entry.spec.intent.intent !== 'color'
    return {
      paint: intent ? { ...paint.binding, lumRange: p.intentLum } : paint.binding,
      thickness: depth.thickness,
      modelScale: depth.scale,
      fake: entry.fake ? p.plateBehind : null,
      rimDir: p.rimDir,
      rimColor: p.rimColor,
      inflate: layer.depth.inflate ?? 0,
      center: [(x0 + x1) / 2, (y0 + y1) / 2],
      radius: Math.max(1e-3, Math.max(x1 - x0, y1 - y0) / 2),
      milkRange: [y0, y1],
      opacity: layer.opacity * paint.opacity,
      lit: p.lit,
    }
  }, [
    paint,
    depth.thickness,
    depth.scale,
    entry.fake,
    entry.spec,
    p.intentLum,
    p.plateBehind,
    p.rimDir,
    p.rimColor,
    p.lit,
    layer.depth.inflate,
    layer.opacity,
    lg.bbox,
  ])

  // ---------------------------------------------------------------- placement
  const drag = useRef<{
    pointerId: number
    startX: number
    startY: number
    clientX: number
    clientY: number
    t: LayerTransform
    moved: boolean
    plane: THREE.Plane
  } | null>(null)
  const override = useRef<LayerTransform | null>(null)

  const place = useCallback(() => {
    const g = groupRef.current
    if (!g) return
    const t = override.current ?? layer.transform
    const s = layerScale(art.scale, t.scale)
    g.position.x = art.x * t.scale + t.x
    g.position.y = art.y * t.scale + t.y
    g.position.z = entry.baseZ + store.explode.current * EXPLODE_SPREAD * (entry.level + 1)
    g.scale.setScalar(s) // uniform, like the worker: bodies are built in local units (thickness / S)
  }, [art, layer.transform, entry.baseZ, entry.level, store])

  const placeRef = useRef(place)
  placeRef.current = place
  useLayoutEffect(() => {
    // The project caught up with the drag result (or placement inputs changed): drop the local override.
    if (!drag.current) override.current = null
    place()
    invalidate()
  }, [place, invalidate])

  useFrame(() => {
    const g = groupRef.current
    if (!g) return
    const z = entry.baseZ + store.explode.current * EXPLODE_SPREAD * (entry.level + 1)
    if (g.position.z !== z) g.position.z = z
  })

  // ---------------------------------------------------------------- interaction
  const interactive = !layer.locked
  const setCursor = (c: string) => {
    gl.domElement.style.cursor = c
  }
  const canDragNow = () => p.view === 'front' && p.draggable && store.explode.current < 0.02

  const onPointerOver = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation()
    store.setHovered(layer.id)
    if (!store.dragging) setCursor(canDragNow() ? 'grab' : 'pointer')
  }
  const onPointerOut = () => {
    if (store.hovered === layer.id) store.setHovered(null)
    if (!store.dragging) setCursor('')
  }
  const onPointerDown = (e: ThreeEvent<PointerEvent>) => {
    if (e.button !== 0) return
    if (p.view === 'orbit' || store.explode.current >= 0.02) return // select on click instead (orbit drags)
    e.stopPropagation()
    p.onSelect(layer.id)
    if (!canDragNow()) return
    const g = groupRef.current
    if (!g) return
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), -g.position.z)
    const hit = e.ray.intersectPlane(plane, new THREE.Vector3())
    if (!hit) return
    ;(e.target as unknown as Element).setPointerCapture(e.pointerId)
    drag.current = {
      pointerId: e.pointerId,
      startX: hit.x,
      startY: hit.y,
      clientX: e.nativeEvent.clientX,
      clientY: e.nativeEvent.clientY,
      t: { ...layer.transform },
      moved: false,
      plane,
    }
  }
  const onPointerMove = (e: ThreeEvent<PointerEvent>) => {
    const d = drag.current
    if (!d || e.pointerId !== d.pointerId) return
    e.stopPropagation()
    if (!d.moved) {
      if (Math.hypot(e.nativeEvent.clientX - d.clientX, e.nativeEvent.clientY - d.clientY) < 3) return
      d.moved = true
      store.setDragging(layer.id)
      setCursor('grabbing')
    }
    const hit = e.ray.intersectPlane(d.plane, new THREE.Vector3())
    if (!hit) return
    let dx = hit.x - d.startX
    let dy = hit.y - d.startY
    if (e.nativeEvent.shiftKey) {
      if (Math.abs(dx) > Math.abs(dy)) dy = 0
      else dx = 0
    }
    let x = d.t.x + dx
    let y = d.t.y + dy
    // Gentle snapping of the layer origin to the canvas axes (hold Alt to disable).
    if (!e.nativeEvent.altKey) {
      const snap = 0.012
      const cx = art.x * d.t.scale + x
      const cy = art.y * d.t.scale + y
      if (Math.abs(cx) < snap) x -= cx
      if (Math.abs(cy) < snap) y -= cy
    }
    override.current = { x, y, scale: d.t.scale }
    place()
    invalidate()
  }
  const endDrag = (e: ThreeEvent<PointerEvent>) => {
    const d = drag.current
    if (!d || e.pointerId !== d.pointerId) return
    e.stopPropagation()
    ;(e.target as unknown as Element).releasePointerCapture?.(e.pointerId)
    drag.current = null
    store.setDragging(null)
    setCursor(store.hovered === layer.id ? 'grab' : '')
    const committed = override.current
    if (d.moved && committed) {
      p.onTransform(layer.id, { ...committed })
      // If the owner does not apply the transform, fall back to the project's value.
      setTimeout(() => {
        if (override.current === committed) {
          override.current = null
          placeRef.current()
          invalidate()
        }
      }, 600)
    } else {
      override.current = null
    }
  }
  const onClick = (e: ThreeEvent<MouseEvent>) => {
    if (e.delta > 4) return
    e.stopPropagation()
    if (p.view === 'orbit' || store.explode.current >= 0.02) p.onSelect(layer.id)
  }

  useEffect(
    () => () => {
      if (store.hovered === layer.id) store.setHovered(null)
      gl.domElement.style.cursor = ''
    },
    [store, layer.id, gl],
  )

  const castShadow = layer.shadow.kind !== 'none' && layer.shadow.opacity > 0.01
  const cards = useMemo(() => {
    // Like the worker: images whose element has no extruded region (no alpha contour) become flat cards.
    const covered = new Set((lg.regions ?? []).map((r) => r.elementId))
    return (lg.images ?? []).filter((c) => c && c.url && !covered.has(c.elementId))
  }, [lg])

  return (
    <group
      ref={groupRef}
      name={`layer:${layer.id}`}
      onPointerOver={interactive ? onPointerOver : undefined}
      onPointerOut={interactive ? onPointerOut : undefined}
      onPointerDown={interactive ? onPointerDown : undefined}
      onPointerMove={interactive ? onPointerMove : undefined}
      onPointerUp={interactive ? endDrag : undefined}
      onPointerCancel={interactive ? endDrag : undefined}
      onClick={interactive ? onClick : undefined}
    >
      {parts.map((part) => (
        <BodyMesh
          key={part.key}
          part={part}
          z={part.zSub / depth.scale}
          layerId={layer.id}
          spec={entry.spec}
          ctx={ctx}
          blend={layer.blendMode}
          castShadow={castShadow}
          order={BLENDED_RENDER_ORDER.layer + entry.level + Math.min(0.9, Math.max(0, part.zSub) * 10)}
          route={entry.routeBlended}
        />
      ))}
      {cards.map((c, i) => (
        <RasterCardMesh
          key={`${c.elementId}:${c.url}:${i}`}
          card={c}
          z={CARD_Z / depth.scale}
          opacity={layer.opacity}
          layerId={layer.id}
          layerMaterial={layer.material}
          presets={p.presets}
          rimDir={p.rimDir}
          lumRange={p.intentLum}
          order={BLENDED_RENDER_ORDER.layer + entry.level + 0.95}
          route={entry.routeBlended}
        />
      ))}
    </group>
  )
})

interface BodyMeshProps {
  part: BodyPart
  /** Local z of the piece (its world zSub / S). */
  z: number
  layerId: string
  spec: IconMaterialSpec
  ctx: MaterialContext
  blend: BlendMode
  castShadow: boolean
  /** renderOrder when the body is blended (drawn back to front, in either pass — see blendInOpaquePass). */
  order: number
  /** Blended bodies go to the opaque pass (StackEntry.routeBlended). */
  route: boolean
}

/** Returns the material's pass key: `mode|transparent|routed|blended` (blended = needs the back-to-front order). */
function applyBlend(m: IconMaterial, blend: BlendMode, route: boolean): string {
  // Blend modes only make sense for opaque, non-refractive bodies; glass always composites physically.
  const mode = m.transmission > 0 ? 'normal' : blend
  m.blending = THREE.NormalBlending
  m.premultipliedAlpha = false
  switch (mode) {
    case 'multiply':
    case 'plus-darker':
      m.blending = THREE.MultiplyBlending
      m.premultipliedAlpha = true
      m.transparent = true
      break
    case 'screen':
    case 'soft-light':
    case 'overlay':
      m.blending = THREE.CustomBlending
      m.blendEquation = THREE.AddEquation
      m.blendSrc = THREE.OneFactor
      m.blendDst = THREE.OneMinusSrcColorFactor
      m.transparent = true
      break
    case 'plus-lighter':
      m.blending = THREE.AdditiveBlending
      m.transparent = true
      break
    case 'darken':
    case 'lighten':
      m.blending = THREE.CustomBlending
      m.blendEquation = mode === 'darken' ? THREE.MinEquation : THREE.MaxEquation
      m.blendSrc = THREE.OneFactor
      m.blendDst = THREE.OneFactor
      m.transparent = true
      break
    default:
      break
  }
  const routed = route && blendInOpaquePass(m)
  return `${mode}|${m.transparent}|${routed}|${routed || m.transparent}`
}

const BodyMesh = memo(function BodyMesh({
  part,
  z,
  layerId,
  spec,
  ctx,
  blend,
  castShadow,
  order,
  route,
}: BodyMeshProps) {
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  const geometry = useCached(geometryCache, part.key, part.build)
  const material = useMemo(() => new IconMaterial(), [])
  const blendKey = useRef('')
  const meshRef = useRef<THREE.Mesh>(null)
  useEffect(() => () => material.dispose(), [material])

  useLayoutEffect(() => {
    applyIconMaterial(material, spec, { ...ctx, opacity: ctx.opacity * part.opacity, paintAlpha: part.alpha })
    const key = applyBlend(material, blend, route)
    if (key !== blendKey.current) {
      blendKey.current = key
      material.needsUpdate = true
    }
    if (meshRef.current) meshRef.current.renderOrder = key.endsWith('|true') ? order : 0
    invalidate()
  }, [material, spec, ctx, part.opacity, part.alpha, blend, order, route, invalidate])

  useLayoutEffect(() => {
    const m = meshRef.current
    if (!m) return
    store.addMesh(layerId, m)
    return () => store.removeMesh(layerId, m)
  }, [store, layerId, geometry])

  // Raster-image regions carry a placeholder paint (solid black) in the bundle: only draw them once their texture
  // (the real pixels + alpha) is available, never as an opaque black body while it loads or after it failed.
  const hidden = part.alpha && !ctx.paint.map
  if (!geometry) return null
  return (
    <mesh
      ref={meshRef}
      geometry={geometry}
      material={material}
      visible={!hidden}
      position-z={z}
      castShadow={castShadow}
      receiveShadow
      renderOrder={blendKey.current.endsWith('|true') ? order : 0}
    />
  )
})

/** Extra placement fields the SVG pipeline writes on raster cards (beyond the RasterCard contract). */
interface CardPlacement {
  matrix?: number[] | null
  width?: number | null
  height?: number | null
}

/**
 * Worker image_quad / image_uv: the card spans the image's full placement (pixel → art affine `matrix`, y down) —
 * `bbox` is only the alpha-traced visible part, so stretching the PNG over it would misplace images with
 * transparent margins. Falls back to the bbox when there is no usable matrix.
 */
export function cardPlacement(card: RasterCard): { quad: [number, number][]; uv: PaintUv } {
  const { matrix: m, width: W, height: H } = card as RasterCard & CardPlacement
  if (m && m.length === 6 && W && H && W > 0 && H > 0 && m.every(Number.isFinite)) {
    const [a, b, c, d, e, f] = m
    const det = a * d - b * c
    if (Math.abs(det) > 1e-18) {
      const corners: [number, number][] = [
        [0, H],
        [W, H],
        [W, 0],
        [0, 0],
      ].map(([px, py]) => [a * px + c * py + e, b * px + d * py + f])
      let area2 = 0
      for (let i = 0; i < 4; i++) {
        const p = corners[(i + 3) % 4]
        const q = corners[i]
        area2 += p[0] * q[1] - q[0] * p[1]
      }
      // pixel = inv(M)·(art − t); u = px / W; v = 1 − py / H (top image row → v = 1, like three's flipY).
      const pxx = d / det
      const pxy = -c / det
      const pxc = (c * f - d * e) / det
      const pyx = -b / det
      const pyy = a / det
      const pyc = (b * e - a * f) / det
      return {
        quad: area2 > 0 ? corners : corners.reverse(),
        uv: [pxx / W, pxy / W, pxc / W, -pyx / H, -pyy / H, 1 - pyc / H],
      }
    }
  }
  const [x0, y0, x1, y1] = card.bbox ?? [-1, -1, 1, 1]
  const w = Math.max(1e-6, x1 - x0)
  const h = Math.max(1e-6, y1 - y0)
  return {
    quad: [
      [x0, y0],
      [x1, y0],
      [x1, y1],
      [x0, y1],
    ],
    uv: [1 / w, 0, -x0 / w, 0, 1 / h, -y0 / h],
  }
}

function cardGeometry(quad: [number, number][]): THREE.BufferGeometry {
  const g = new THREE.BufferGeometry()
  const pos = new Float32Array(12)
  quad.forEach(([x, y], i) => pos.set([x, y, 0], i * 3))
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3))
  g.setAttribute('normal', new THREE.BufferAttribute(new Float32Array([0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1]), 3))
  g.setIndex([0, 1, 2, 0, 2, 3])
  g.computeBoundingSphere()
  return g
}

/** The worker renders cards with the unlit `flat` preset, keeping the layer's appearance intent (mono / tint). */
function cardMaterialSpec(layerMaterial: MaterialSpec): MaterialSpec {
  const params: MaterialSpec['params'] = {}
  for (const [k, v] of Object.entries(layerMaterial.params ?? {})) if (k.startsWith('__')) params[k] = v
  return { preset: 'flat', params }
}

const CARD_FALLBACK = new THREE.Color(0.5, 0.5, 0.5)

function RasterCardMesh({
  card,
  z,
  opacity,
  layerId,
  layerMaterial,
  presets,
  rimDir,
  lumRange,
  order,
  route,
}: {
  card: RasterCard
  z: number
  opacity: number
  layerId: string
  layerMaterial: MaterialSpec
  presets: Presets | null
  rimDir: THREE.Vector3
  lumRange: [number, number]
  order: number
  /** Opaque-pass routing (StackEntry.routeBlended). */
  route: boolean
}) {
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  const asset = useTextureAsset(card.url || null)
  const placement = useMemo(() => cardPlacement(card), [card])
  const geometry = useMemo(() => cardGeometry(placement.quad), [placement])
  const material = useMemo(() => new IconMaterial(), [])
  const spec = useMemo(() => describeMaterial(cardMaterialSpec(layerMaterial), presets), [layerMaterial, presets])
  const meshRef = useRef<THREE.Mesh>(null)
  const routedRef = useRef<boolean | null>(null)
  useEffect(() => () => geometry.dispose(), [geometry])
  useEffect(() => () => material.dispose(), [material])
  const ready = !!asset?.ready
  useLayoutEffect(() => {
    applyIconMaterial(material, spec, {
      paint: { map: ready ? asset!.texture : null, color: CARD_FALLBACK, lumRange, uv: placement.uv },
      thickness: 0.004,
      fake: null,
      rimDir,
      opacity: ready ? opacity * (card.opacity ?? 1) : 0,
      paintAlpha: true,
    })
    material.transparent = true
    material.depthWrite = false
    material.blending = THREE.NormalBlending // undo an earlier routing
    // Under refracting glass: opaque pass, so it stays visible (and refracted); on top of glass: after it.
    const routed = route && blendInOpaquePass(material)
    if (routed !== routedRef.current) {
      routedRef.current = routed
      material.needsUpdate = true
    }
    invalidate()
  }, [material, spec, asset, ready, placement, lumRange, rimDir, opacity, card.opacity, route, invalidate])
  useLayoutEffect(() => {
    const m = meshRef.current
    if (!m) return
    store.addMesh(layerId, m)
    return () => store.removeMesh(layerId, m)
  }, [store, layerId])
  return <mesh ref={meshRef} geometry={geometry} material={material} position-z={z} renderOrder={order} />
}
