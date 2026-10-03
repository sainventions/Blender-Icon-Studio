// Instant three.js preview of the icon — a lighting/layout draft that mirrors the Blender scene (PLAN §3/§4 D2, D7).
import { useCallback, useMemo, useRef, type CSSProperties, type JSX } from 'react'
import { Canvas, type RootState } from '@react-three/fiber'
import * as THREE from 'three'
import type { AppearanceId, GeometryBundle, LayerTransform, Presets, Project } from '../types'
import { resolveAppearance } from '../lib/appearance'
import { SceneRoot } from './scene/SceneRoot'
import { StoreContext, ViewportStore } from './scene/store'

export interface ViewportProps {
  project: Project
  geometry: GeometryBundle | null
  presets: Presets
  appearance: AppearanceId
  selectedLayerId: string | null
  onSelectLayer: (id: string | null) => void
  /** Drag to move in the front view (optional). Called once per drag, on release. */
  onLayerTransform?: (id: string, t: LayerTransform) => void
  /** 0..1 UI explode amount (spreads z gaps, swings the camera into a three-quarter view). */
  explode: number
  view: 'front' | 'orbit'
  /** Apple-style icon grid overlay. */
  showGrid?: boolean
  /**
   * The element around the viewport whose background is lib/stageBackdrop's stage CSS (the editor stage). With the
   * transparent backdrop the canvas then repaints exactly that background under its faded checkerboard, so the live
   * view blends into the page; without it the checker fades into the stage's flat base colour.
   */
  stageElement?: () => HTMLElement | null
  className?: string
}

/** Used when the caller passes no className: fill the parent. */
const OUTER_DEFAULT: CSSProperties = { position: 'relative', width: '100%', height: '100%', minWidth: 0, minHeight: 0 }
/** The caller's className owns position/size; the inner box always fills it. */
const INNER_STYLE: CSSProperties = {
  position: 'relative',
  width: '100%',
  height: '100%',
  minWidth: 0,
  minHeight: 0,
  overflow: 'hidden',
  background: 'transparent', // until the first frame; the backdrop then repaints the stage behind the canvas
  touchAction: 'none',
}

const PILL_STYLE: CSSProperties = {
  position: 'absolute',
  left: '50%',
  bottom: 14,
  transform: 'translateX(-50%)',
  padding: '5px 11px',
  borderRadius: 999,
  font: '500 11px/16px Inter, "Segoe UI", system-ui, sans-serif',
  color: 'rgba(236,236,241,0.78)',
  background: 'rgba(18,18,23,0.72)',
  boxShadow: '0 0 0 1px rgba(255,255,255,0.07), 0 8px 24px -8px rgba(0,0,0,0.6)',
  backdropFilter: 'blur(8px)',
  pointerEvents: 'none',
  whiteSpace: 'nowrap',
}

function onCreated(state: RootState, store: ViewportStore): void {
  const { gl } = state
  // Transmission samples a copy of the opaque scene; on HiDPI screens half resolution is indistinguishable.
  gl.transmissionResolutionScale = Math.max(0.5, Math.min(1, 1.25 / gl.getPixelRatio()))
  gl.shadowMap.type = THREE.VSMShadowMap
  if (import.meta.env.DEV) {
    ;(window as unknown as { __bisViewport?: unknown }).__bisViewport = {
      gl,
      scene: state.scene,
      invalidate: state.invalidate,
      store,
      /**
       * Render `frames` frames synchronously (works while the page is hidden / rAF is throttled). Each frame is
       * stepped as 1/60 s of simulated time so damped animations (explode, camera framing) progress.
       */
      advance(frames = 60) {
        const t0 = performance.now()
        for (let i = 0; i < frames; i++) {
          const s = state.get()
          s.clock.oldTime = performance.now() - 1000 / 60
          s.advance(t0 + i * 16.67, true)
        }
      },
      get camera() {
        return state.get().camera
      },
      get controls() {
        return state.get().controls
      },
    }
  }
}

export function Viewport(p: ViewportProps): JSX.Element {
  const store = useMemo(() => new ViewportStore(), [])
  const handleCreated = useCallback((state: RootState) => onCreated(state, store), [store])
  const effective = useMemo(() => resolveAppearance(p.project, p.appearance), [p.project, p.appearance])
  // watchOS ignores appearances (always light) — keep the backdrop consistent with resolveAppearance.
  const appearance = p.project.canvas.platform === 'watchos' ? 'light' : p.appearance
  const selectRef = useRef(p.onSelectLayer)
  selectRef.current = p.onSelectLayer
  const onMissed = useCallback(() => selectRef.current(null), [])

  const pending = !p.geometry && effective.layers.some((l) => l.visible) ? 'Building geometry…' : null

  return (
    <div className={p.className} style={p.className ? undefined : OUTER_DEFAULT} data-bis-viewport="">
      <div style={INNER_STYLE}>
        <Canvas
          frameloop="demand"
          shadows="variance"
          dpr={[1, 2]}
          gl={{ antialias: false, alpha: false, stencil: false, powerPreference: 'high-performance' }}
          onCreated={handleCreated}
          onPointerMissed={onMissed}
          style={{ position: 'absolute', inset: 0 }}
        >
          <StoreContext.Provider value={store}>
            <SceneRoot
              project={effective}
              appearance={appearance}
              geometry={p.geometry}
              presets={p.presets ?? null}
              selectedLayerId={p.selectedLayerId}
              onSelectLayer={p.onSelectLayer}
              onLayerTransform={p.onLayerTransform}
              explode={p.explode}
              view={p.view}
              showGrid={!!p.showGrid}
              stage={p.stageElement}
            />
          </StoreContext.Provider>
        </Canvas>
        {pending && <div style={PILL_STYLE}>{pending}</div>}
      </div>
    </div>
  )
}
