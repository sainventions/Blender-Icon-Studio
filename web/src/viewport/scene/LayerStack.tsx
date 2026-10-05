// The layer stack: one group per visible layer (canvas.art ∘ layer.transform, z = depth.z + ε at REAL distances,
// PLAN §11), one height-field body per region ('individual') or the union silhouette ('combined' / touching opaque
// pieces), flat raster cards, and ONE Principled material per shape (layer material merged with
// Layer.elementMaterials). Picking, hover and drag-to-move (head-on view only). The stack itself is built in ./stack.ts.
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useThree, type ThreeEvent } from '@react-three/fiber'
import * as THREE from 'three'
import type { ArtTransform, LayerTransform } from '../../types'
import {
  applyPrincipled,
  BLENDED_RENDER_ORDER,
  PrincipledMaterial,
  type MonoParams,
  type PaintBinding,
  type Principled,
} from '../../lib/materials3d'
import { bodyCache, imageUv, layerLift, layerScale, partShifts, type Body, type BodyPart } from '../geometry/layerGeometry'
import { fillPreviewColor } from '../textures/fillTextures'
import { useTextureAsset } from '../textures/layerTextures'
import type { StackEntry } from './stack'
import { useViewportStore } from './store'
import { usePaint } from './usePaint'

export { buildStack, stackFramePoints, stackTop } from './stack'
export type { PlateFrame, StackEntry } from './stack'

/** Bodies of the parts, built (memoised) during render and held while mounted. */
function useBodies(parts: BodyPart[]): Body[] {
  const bodies = useMemo(() => parts.map((p) => bodyCache.get(p.key, p.build)), [parts])
  useEffect(() => {
    for (const p of parts) {
      bodyCache.get(p.key, p.build)
      bodyCache.retain(p.key)
    }
    return () => {
      for (const p of parts) bodyCache.release(p.key)
    }
  }, [parts])
  return bodies
}

interface LayerBodyProps {
  entry: StackEntry
  art: ArtTransform
  view: 'front' | 'orbit'
  draggable: boolean
  onSelect: (id: string | null) => void
  onTransform: (id: string, t: LayerTransform) => void
  /** Tinted renditions: the worker's env mono (art luminance × tint), null otherwise. */
  mono: MonoParams | null
  /** What a covered glass body shows through itself (linear colour; see ShapeContext.covered). */
  behind: THREE.Color
}

export const LayerBody = memo(function LayerBody(p: LayerBodyProps) {
  const { entry, art } = p
  const { layer, lg } = entry
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  const gl = useThree((s) => s.gl)
  const groupRef = useRef<THREE.Group>(null)

  const bodies = useBodies(entry.parts)
  const lift = useMemo(
    () => layerLift(entry.parts.map((part, i) => ({ part, body: bodies[i] })), entry.depth),
    [entry.parts, bodies, entry.depth],
  )
  // local z of every body's mid-plane: one shared mid-plane (+ zSub) lifted for inflate, overlapping pieces of the
  // layer stacked by their real heights (worker scene._layer: heightfield.stack_shifts)
  const partZ = useMemo(() => {
    const base = entry.parts.map((part) => (part.card ? part.z : part.z + lift))
    const sh = partShifts(entry.pieceStack, bodies, base, entry.scale)
    return base.map((z, i) => z + sh[i])
  }, [entry.parts, entry.pieceStack, bodies, lift, entry.scale])
  const fallback = useMemo(() => {
    const r = lg.regions?.[0]
    return fillPreviewColor(layer.fill.type === 'auto' && r ? r.paint : layer.fill, '#c8ccd6')
  }, [layer.fill, lg])
  const paint = usePaint(layer.fill, lg.texture || null, fallback)

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
    g.position.set(art.x * t.scale + t.x, art.y * t.scale + t.y, entry.z)
    g.scale.setScalar(layerScale(art.scale, t.scale)) // uniform, like the worker: bodies are built in local units
  }, [art, layer.transform, entry.z])

  const placeRef = useRef(place)
  placeRef.current = place
  useLayoutEffect(() => {
    // The project caught up with the drag result (or placement inputs changed): drop the local override.
    if (!drag.current) override.current = null
    place()
    invalidate()
  }, [place, invalidate])

  // ---------------------------------------------------------------- interaction
  const interactive = !layer.locked
  const setCursor = (c: string) => {
    gl.domElement.style.cursor = c
  }
  const headOn = () => p.view === 'front' && store.iso.current < 0.02
  const canDragNow = () => headOn() && p.draggable

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
    if (!headOn()) return // select on click instead (orbit drags, iso view)
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
    if (!headOn()) p.onSelect(layer.id)
  }

  useEffect(
    () => () => {
      if (store.hovered === layer.id) store.setHovered(null)
      gl.domElement.style.cursor = ''
    },
    [store, layer.id, gl],
  )

  const castShadow = layer.shadow?.kind !== 'none'
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
      {entry.parts.map((part, i) => (
        <BodyMesh
          key={part.key}
          part={part}
          body={bodies[i]}
          z={partZ[i]}
          layerId={layer.id}
          params={entry.params.get(part.key)!}
          paint={paint.binding}
          opacity={layer.opacity * part.opacity}
          thickness={part.card ? 0.004 / entry.scale : entry.depth.thickness}
          mono={p.mono}
          covered={entry.glassAbove ? p.behind : null}
          contact={entry.contact}
          bbox={lg.bbox}
          castShadow={castShadow}
          order={BLENDED_RENDER_ORDER.layer + entry.level + (part.card ? 0.95 : Math.min(0.9, Math.max(0, part.z) * 10))}
          route={entry.routeBlended}
        />
      ))}
    </group>
  )
})

interface BodyMeshProps {
  part: BodyPart
  body: Body
  /** Local z of the body's mid-plane (incl. the layer lift). */
  z: number
  layerId: string
  params: Principled
  /** The layer's paint (pieces with a raster image of their own use that instead). */
  paint: PaintBinding
  opacity: number
  /** Local thickness (three.js transmission thickness). */
  thickness: number
  mono: MonoParams | null
  covered: THREE.Color | null
  /** StackEntry.contact: the surface seen through the body is lit through it. */
  contact: number
  bbox: [number, number, number, number]
  castShadow: boolean
  /** renderOrder when the body is drawn translucent in the opaque pass. */
  order: number
  route: boolean
}

const CARD_FALLBACK = new THREE.Color(0.5, 0.5, 0.5)

const BodyMesh = memo(function BodyMesh(p: BodyMeshProps) {
  const store = useViewportStore()
  const invalidate = useThree((s) => s.invalidate)
  const material = useMemo(() => new PrincipledMaterial(), [])
  const meshRef = useRef<THREE.Mesh>(null)
  useEffect(() => () => material.dispose(), [material])
  const image = p.part.image
  const asset = useTextureAsset(image?.url || null)
  const imageReady = !!asset?.ready
  const uv = useMemo(() => (image ? imageUv(image, p.bbox) : null), [image, p.bbox])

  useLayoutEffect(() => {
    const paint: PaintBinding = image
      ? {
          map: imageReady ? asset!.texture : null,
          color: asset?.stats ? new THREE.Color(...asset.stats.avg) : CARD_FALLBACK,
          uv,
          alpha: true,
        }
      : p.paint
    applyPrincipled(material, p.params, {
      paint,
      opacity: image ? p.opacity * (image.opacity ?? 1) : p.opacity,
      thickness: p.thickness,
      mono: p.mono,
      covered: p.covered,
      route: p.route,
      contact: p.contact,
    })
    if (meshRef.current) meshRef.current.renderOrder = material.routed ? p.order : 0
    invalidate()
  }, [material, p.params, p.paint, p.opacity, p.thickness, p.mono, p.covered, p.contact, p.route, p.order, image, imageReady, asset, uv, invalidate])

  useLayoutEffect(() => {
    const m = meshRef.current
    if (!m) return
    store.addMesh(p.layerId, m)
    return () => store.removeMesh(p.layerId, m)
  }, [store, p.layerId, p.body])

  // Raster pieces carry a placeholder paint in the bundle: only draw them once their own pixels (+ alpha) are loaded.
  const hidden = !!image && !imageReady
  return (
    <mesh
      ref={meshRef}
      geometry={p.body.geometry}
      material={material}
      visible={!hidden}
      position-z={p.z}
      castShadow={p.castShadow}
      receiveShadow
      renderOrder={material.routed ? p.order : 0}
    />
  )
})

