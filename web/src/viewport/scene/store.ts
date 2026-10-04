// Per-viewport mutable state that must not re-render the whole scene: hover, the mesh registry used by the
// selection/hover outlines, animated values (the CAD iso view amount) and the active drag.
import { createContext, useContext } from 'react'
import type * as THREE from 'three'

type Listener = () => void

export class ViewportStore {
  hovered: string | null = null
  /** layerId → meshes of that layer (outline selection sets). */
  readonly meshes = new Map<string, Set<THREE.Mesh>>()
  /** Animated CAD iso view amount (0 = head-on .. 1 = isometric), damped toward `target` every frame. */
  readonly iso = { current: 0, target: 0 }
  /** Layer being dragged (front view) — suppresses hover outlines while moving. */
  dragging: string | null = null
  version = 0

  private listeners = new Set<Listener>()
  private pending = false

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn)
    return () => {
      this.listeners.delete(fn)
    }
  }

  getVersion = (): number => this.version

  setHovered(id: string | null): void {
    if (this.hovered === id) return
    this.hovered = id
    this.bump()
  }

  setDragging(id: string | null): void {
    if (this.dragging === id) return
    this.dragging = id
    this.bump()
  }

  addMesh(layerId: string, mesh: THREE.Mesh): void {
    let set = this.meshes.get(layerId)
    if (!set) {
      set = new Set()
      this.meshes.set(layerId, set)
    }
    set.add(mesh)
    this.bump()
  }

  removeMesh(layerId: string, mesh: THREE.Mesh): void {
    const set = this.meshes.get(layerId)
    if (!set) return
    set.delete(mesh)
    if (!set.size) this.meshes.delete(layerId)
    this.bump()
  }

  meshesOf(layerId: string | null): THREE.Mesh[] {
    if (!layerId) return []
    return [...(this.meshes.get(layerId) ?? [])]
  }

  private bump(): void {
    this.version++
    if (this.pending) return
    this.pending = true
    queueMicrotask(() => {
      this.pending = false
      this.listeners.forEach((fn) => fn())
    })
  }
}

export const StoreContext = createContext<ViewportStore | null>(null)

export function useViewportStore(): ViewportStore {
  const s = useContext(StoreContext)
  if (!s) throw new Error('useViewportStore outside <Viewport>')
  return s
}

/** Frame-rate independent exponential damping. Returns the new value. */
export function damp(current: number, target: number, lambda: number, dt: number): number {
  // dt is capped: in demand-driven rendering the first frame after idle reports the whole idle time.
  return target + (current - target) * Math.exp(-lambda * Math.min(dt, 1 / 30))
}
