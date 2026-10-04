// Apple-style icon grid (golden-ratio keyline circles, tangent guides, diagonals, the plate outline) drawn in canvas
// space on top of everything. It lives in the 3D scene so it follows the camera in orbit / iso views.
// Platform extras: Android adaptive safe zone (72/108), macOS body inset, watchOS circle.
import { useEffect, useMemo } from 'react'
import * as THREE from 'three'
import type { Platform, PlateShape } from '../../types'
import { plateOutline } from '../../lib/shapes'

interface Props {
  shape: PlateShape
  cornerRadius: number
  platform: Platform
  /** z of the overlay (just above the front of the stack: real distances). */
  z: number
}

const PHI = (1 + Math.sqrt(5)) / 2

function circle(r: number, n = 128, cx = 0, cy = 0): number[] {
  const out: number[] = []
  for (let i = 0; i < n; i++) {
    const a0 = (i / n) * Math.PI * 2
    const a1 = ((i + 1) / n) * Math.PI * 2
    out.push(cx + Math.cos(a0) * r, cy + Math.sin(a0) * r, 0, cx + Math.cos(a1) * r, cy + Math.sin(a1) * r, 0)
  }
  return out
}

function polyline(pts: [number, number][], closed = true): number[] {
  const out: number[] = []
  const n = pts.length
  for (let i = 0; i < (closed ? n : n - 1); i++) {
    const a = pts[i]
    const b = pts[(i + 1) % n]
    out.push(a[0], a[1], 0, b[0], b[1], 0)
  }
  return out
}

function seg(x0: number, y0: number, x1: number, y1: number): number[] {
  return [x0, y0, 0, x1, y1, 0]
}

function makeLines(positions: number[], color: string, opacity: number): THREE.LineSegments {
  const g = new THREE.BufferGeometry()
  g.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  const m = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthTest: false,
    depthWrite: false,
    toneMapped: false,
  })
  const l = new THREE.LineSegments(g, m)
  l.renderOrder = 1000
  l.raycast = () => {}
  l.frustumCulled = false
  return l
}

export function GridOverlay({ shape, cornerRadius, platform, z }: Props) {
  const group = useMemo(() => {
    const r1 = 0.86
    const r2 = r1 / PHI
    const r3 = r2 / PHI
    const major: number[] = [
      ...circle(r1),
      ...circle(r2),
      ...circle(r3),
      ...seg(-1, 0, 1, 0),
      ...seg(0, -1, 0, 1),
      ...seg(-1, -1, 1, 1),
      ...seg(-1, 1, 1, -1),
    ]
    const minor: number[] = []
    for (const r of [r2, r3]) {
      minor.push(...seg(-r, -1, -r, 1), ...seg(r, -1, r, 1), ...seg(-1, -r, 1, -r), ...seg(-1, r, 1, r))
    }
    minor.push(
      ...polyline([
        [-r1, -r1],
        [r1, -r1],
        [r1, r1],
        [-r1, r1],
      ]),
    )
    const accent: number[] = []
    const outline = plateOutline(shape === 'none' ? 'square' : shape, cornerRadius, 192)
    accent.push(...polyline(outline))
    if (platform === 'android') accent.push(...circle(72 / 108, 128)) // adaptive icon safe zone
    if (platform === 'macos') {
      const s = 0.8047
      accent.push(...polyline(plateOutline('squircle', 0, 192).map(([x, y]) => [x * s, y * s] as [number, number])))
    }
    const g = new THREE.Group()
    g.add(makeLines(minor, '#ffffff', 0.12), makeLines(major, '#ffffff', 0.24), makeLines(accent, '#8f7dff', 0.55))
    g.name = 'grid-overlay'
    return g
  }, [shape, cornerRadius, platform])

  useEffect(
    () => () => {
      group.traverse((o) => {
        const l = o as THREE.LineSegments
        if (l.isLineSegments) {
          l.geometry.dispose()
          ;(l.material as THREE.Material).dispose()
        }
      })
    },
    [group],
  )

  return <primitive object={group} position-z={z} />
}
