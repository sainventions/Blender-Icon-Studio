// What the camera sees behind the icon: the stage's "transparent" checkerboard, a solid colour or the worker's
// wallpaper (clear / tinted renditions). It is part of the scene, so transmissive glass refracts it too.
//
// Transparent backdrop: a full-screen shader quad repaints the editor stage behind the canvas (lib/stageBackdrop:
// base colour, accent glow and dot grid, located from the stage element's rect) with the Render view's radially
// faded checkerboard on top. It is drawn in *scene* colours run through the inverse of the viewport's display
// transform (displayTransform.ts), so after tone mapping the canvas edge matches the CSS stage pixel for pixel and no
// box shows around the live view (Khronos PBR Neutral alone turns a #17171c checker into ~#02020c).
//
// The wallpaper is defined in world units (like the worker's wallpaper plane behind the plate), so the background
// texture is framed to match the front camera (ortho_scale 2.24 / zoom across the icon frame — the editor's view
// window, store.frame — so it zooms and pans with the icon).
//
// What REFRACTED light sees behind the icon (round 7): in Cycles only camera rays see the backdrop colour / the
// transparent film — rays refracted through glass reach the studio world. three.js refracts by looking up its
// transmission pass (the scene rendered behind the glass), so without help, glass floating above the plate in the iso
// view (Photos' petals on a real-height stack) showed the dark stage and rendered black. TransmissionWorld draws the
// studio environment behind everything in that pass only (camera-relative like the lights: the screen spans a wide
// cone behind the icon), so floating glass refracts the studio like in Cycles. The wallpaper renditions keep their
// wallpaper (the worker's wallpaper plane is real geometry behind the plate).
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'
import { checkerOver, STAGE_BACKDROP, type StageBackdropSpec } from '../../lib/stageBackdrop'
import { createWallpaperTexture, WALLPAPER_EXTENT } from '../textures/procedural'
import { DISPLAY_INVERSE_GLSL, toScene, type DisplayTransform } from './displayTransform'
import { resolveFrame, useViewportStore } from './store'

export type BackdropSpec =
  { kind: 'checker' } | { kind: 'color'; color: string } | { kind: 'wallpaper'; tone: 'light' | 'dark' }

export interface BackdropBinding {
  spec: BackdropSpec
  texture: THREE.Texture | null
  /** Representative scene-linear colour (what a covered glass plate shows through itself). */
  color: THREE.Color
}

/** Returns the element whose CSS background (lib/stageBackdrop) the transparent backdrop continues, if any. */
export type StageElementGetter = () => HTMLElement | null

/** Front framing (must equal CameraRig's FRONT_ORTHO_SCALE). */
const FRONT_ORTHO_SCALE = 2.24

export function backdropKey(spec: BackdropSpec): string {
  return spec.kind === 'color' ? `color:${spec.color}` : spec.kind === 'wallpaper' ? `wall:${spec.tone}` : 'checker'
}

const srgbToLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)

/**
 * Scene-linear colour of the transparent backdrop around the icon (stage base under the checker at its centre
 * opacity, both checker colours averaged).
 */
export function checkerBehindColor(display: DisplayTransform, spec: StageBackdropSpec = STAGE_BACKDROP): THREE.Color {
  const base = spec.base.map((v) => v / 255) as [number, number, number]
  // Two neighbouring cells at the frame centre (full checker opacity there).
  const c = spec.checker.cell
  const a = checkerOver(base, 50 * c + 0.5, 50 * c + 0.5, 100 * c, 100 * c, spec)
  const b = checkerOver(base, 51 * c + 0.5, 50 * c + 0.5, 100 * c, 100 * c, spec)
  const mid = a.map((v, i) => srgbToLinear((v + b[i]) / 2)) as [number, number, number]
  const s = toScene(mid, display)
  return new THREE.Color(s[0], s[1], s[2])
}

/** Representative (scene-linear) colour of a rendition wallpaper. */
export function wallpaperColor(tone: 'light' | 'dark'): THREE.Color {
  return new THREE.Color(tone === 'light' ? '#e9eafa' : '#0a1024')
}

/** Creates (and owns) the backdrop texture for `spec`. */
export function useBackdropBinding(spec: BackdropSpec, display: DisplayTransform): BackdropBinding {
  const key = backdropKey(spec)
  const displayKey = spec.kind === 'checker' ? `${display.mode}:${display.saturation}` : ''
  const binding = useMemo<BackdropBinding>(() => {
    if (spec.kind === 'checker') return { spec, texture: null, color: checkerBehindColor(display) }
    if (spec.kind === 'wallpaper') return { spec, texture: createWallpaperTexture(spec.tone), color: wallpaperColor(spec.tone) }
    return { spec, texture: null, color: new THREE.Color(spec.color) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, displayKey])
  useEffect(() => () => binding.texture?.dispose(), [binding])
  return binding
}

export function Backdrop({
  binding,
  zoom,
  display,
  stage,
}: {
  binding: BackdropBinding
  zoom: number
  display: DisplayTransform
  stage?: StageElementGetter
}) {
  const scene = useThree((s) => s.scene)
  const size = useThree((s) => s.size)
  const invalidate = useThree((s) => s.invalidate)
  const checker = binding.spec.kind === 'checker'

  useLayoutEffect(() => {
    if (checker) return
    scene.background = binding.texture ?? binding.color
    invalidate()
    return () => {
      if (scene.background === binding.texture || scene.background === binding.color) scene.background = null
    }
  }, [scene, binding, checker, invalidate])

  // Frame the wallpaper like the front camera: world ±FRONT_ORTHO_SCALE/2/zoom across the icon frame (the editor's
  // CAD zoom / pan view window, store.frame; else the canvas's centred square), so it zooms and pans with the icon.
  // Runs before each render (a view change only invalidates a frame).
  const store = useViewportStore()
  const fitted = useRef('')
  useFrame(() => {
    const t = binding.texture
    if (!t || binding.spec.kind !== 'wallpaper') return
    const w = Math.max(1, size.width)
    const h = Math.max(1, size.height)
    const vf = resolveFrame(store.frame, w, h)
    const key = `${t.uuid}|${w}|${h}|${vf.x}|${vf.y}|${vf.side}|${zoom}`
    if (key === fitted.current) return
    fitted.current = key
    const upp = FRONT_ORTHO_SCALE / Math.max(0.05, zoom || 1) / vf.side // world units per CSS px
    const x0 = -(vf.x + vf.side / 2) * upp // canvas left edge (world x)
    const y0 = -(h - vf.y - vf.side / 2) * upp // canvas bottom edge (world y)
    // the texture covers world ±WALLPAPER_EXTENT (clamped at its edges beyond that)
    const span = 2 * WALLPAPER_EXTENT
    t.repeat.set((w * upp) / span, (h * upp) / span)
    t.offset.set((x0 + WALLPAPER_EXTENT) / span, (y0 + WALLPAPER_EXTENT) / span)
    t.updateMatrix()
  })

  return (
    <>
      {checker && <StageCheckerBackdrop display={display} stage={stage} />}
      {binding.spec.kind !== 'wallpaper' && <TransmissionWorld />}
    </>
  )
}

// ------------------------------------------------------------------------------------------ transmission-pass world
/** Half field of the cone behind the icon the canvas spans in the transmission pass: tan(45°) across half its height. */
export const TRANSMISSION_WORLD_SPREAD = 1.0

const WORLD_FRAG = /* glsl */ `
varying vec2 vBisUv;
uniform samplerCube uEnv;
uniform float uPass;      // 1 while three.js renders its transmission pass, else 0 (nothing drawn)
uniform mat3 uEnvView;    // camera frame -> environment cube frame (environmentRotation^T × camera rotation)
uniform vec2 uSpread;     // tan of the half field across the canvas (x: × aspect)
uniform float uIntensity; // scene.environmentIntensity
void main() {
  if (uPass < 0.5) discard;
  // the direction behind the icon through this pixel (camera frame: x right, y up, looking along −z)
  vec3 d = normalize(vec3((vBisUv * 2.0 - 1.0) * uSpread, -1.0));
  gl_FragColor = vec4(textureCube(uEnv, uEnvView * d).rgb * uIntensity, 1.0);
}
`

/** three.js' transmission render target (mip-mapped for the roughness blur; every other target in the app is not). */
export function isTransmissionTarget(rt: THREE.WebGLRenderTarget | null): boolean {
  return !!rt && rt.texture.generateMipmaps === true && rt.texture.minFilter === THREE.LinearMipmapLinearFilter
}

const _m4 = new THREE.Matrix4()
const _env = new THREE.Matrix3()
const _cam = new THREE.Matrix3()

/** Full-screen quad drawn only in the transmission pass: the studio environment behind the icon (see above). */
function TransmissionWorld() {
  const mesh = useMemo(() => {
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(new Array(12).fill(0), 3))
    geometry.setAttribute('bisClip', new THREE.Float32BufferAttribute([-1, -1, 1, -1, 1, 1, -1, 1], 2))
    geometry.setIndex([0, 1, 2, 0, 2, 3])
    const material = new THREE.ShaderMaterial({
      name: 'BisTransmissionWorld',
      vertexShader: VERT,
      fragmentShader: WORLD_FRAG,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
      uniforms: {
        uEnv: { value: null },
        uPass: { value: 0 },
        uEnvView: { value: new THREE.Matrix3() },
        uSpread: { value: new THREE.Vector2(1, 1) },
        uIntensity: { value: 1 },
      },
    })
    const m = new THREE.Mesh(geometry, material)
    m.name = 'bis-transmission-world'
    m.frustumCulled = false
    m.renderOrder = -1e9 + 1 // right after the stage backdrop; no depth, so the icon always draws over it
    m.matrixAutoUpdate = false
    m.raycast = () => {}
    m.onBeforeRender = (renderer, scene, camera) => {
      const u = material.uniforms
      const rt = renderer.getRenderTarget()
      const pass = isTransmissionTarget(rt) && !!scene.environment
      u.uPass.value = pass ? 1 : 0
      if (pass && rt) {
        u.uEnv.value = scene.environment
        // three.js samples the environment with environmentRotation^T (WebGLMaterials envMapRotation)
        _env.setFromMatrix4(_m4.makeRotationFromEuler(scene.environmentRotation)).transpose()
        _cam.setFromMatrix4(camera.matrixWorld)
        ;(u.uEnvView.value as THREE.Matrix3).multiplyMatrices(_env, _cam)
        const aspect = rt.width / Math.max(1, rt.height)
        ;(u.uSpread.value as THREE.Vector2).set(TRANSMISSION_WORLD_SPREAD * aspect, TRANSMISSION_WORLD_SPREAD)
        u.uIntensity.value = scene.environmentIntensity
      }
      material.uniformsNeedUpdate = true
    }
    return m
  }, [])
  useEffect(
    () => () => {
      mesh.geometry.dispose()
      ;(mesh.material as THREE.Material).dispose()
    },
    [mesh],
  )
  return <primitive object={mesh} />
}

// ------------------------------------------------------------------------------------------ stage checker backdrop
const VERT = /* glsl */ `
attribute vec2 bisClip;
varying vec2 vBisUv;
void main() {
  // Full-screen quad straight in clip space. \`position\` is all zeros, so passes that render the scene with an
  // override material (depth / outline masks) rasterise nothing for this mesh.
  vBisUv = bisClip * 0.5 + 0.5;
  gl_Position = vec4(bisClip + position.xy, 0.0, 1.0);
}
`

const FRAG = /* glsl */ `
varying vec2 vBisUv;
uniform vec2 uCanvas;   // canvas size, CSS px
uniform vec4 uStage;    // stage box relative to the canvas top-left (x, y, w, h; CSS px); w = 0: no stage
uniform vec4 uFrame;    // icon frame (the checker's box) relative to the canvas top-left (x, y, w, h; CSS px)
uniform vec3 uBase;     // sRGB 0..1
uniform vec4 uGlow;     // rgb (sRGB), alpha
uniform vec4 uGlowBox;  // cx, cy, rx, ry (fractions of the stage box)
uniform float uGlowStop;
uniform vec4 uDots;     // spacing, alpha, inner, outer (CSS px)
uniform vec3 uCheckA;
uniform vec3 uCheckB;
uniform vec2 uCheck;    // cell (CSS px), opacity
${DISPLAY_INVERSE_GLSL}
vec3 bisSrgbToLinear(vec3 c) {
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}
void main() {
  vec2 p = vec2(vBisUv.x, 1.0 - vBisUv.y) * uCanvas; // CSS px from the canvas top-left (y down, like CSS)
  vec3 col = uBase;
  if (uStage.z > 0.0) {
    vec2 s = p - uStage.xy;
    vec2 m = mod(s, uDots.x) - 0.5 * uDots.x;
    float aDot = uDots.y * clamp((uDots.w - length(m)) / (uDots.w - uDots.z), 0.0, 1.0);
    col = mix(col, vec3(1.0), aDot);
    vec2 q = (s - uGlowBox.xy * uStage.zw) / (uGlowBox.zw * uStage.zw);
    float aGlow = uGlow.a * clamp(1.0 - length(q) / uGlowStop, 0.0, 1.0);
    col = mix(col, uGlow.rgb, aGlow);
  }
  vec2 f = p - uFrame.xy;
  vec2 cell = floor(f / uCheck.x);
  vec3 sq = mod(cell.x + cell.y, 2.0) > 0.5 ? uCheckA : uCheckB;
  vec2 r = (f - 0.5 * uFrame.zw) / (0.5 * uFrame.zw);
  col = mix(col, sq, uCheck.y * clamp(1.0 - length(r), 0.0, 1.0));
  gl_FragColor = vec4(bisToScene(bisSrgbToLinear(col)), 1.0);
}
`

const rgb01 = (c: readonly number[]) => new THREE.Vector3(c[0] / 255, c[1] / 255, c[2] / 255)

function StageCheckerBackdrop({ display, stage }: { display: DisplayTransform; stage?: StageElementGetter }) {
  const gl = useThree((s) => s.gl)
  const size = useThree((s) => s.size)
  const invalidate = useThree((s) => s.invalidate)

  const mesh = useMemo(() => {
    const spec = STAGE_BACKDROP
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(new Array(12).fill(0), 3))
    geometry.setAttribute('bisClip', new THREE.Float32BufferAttribute([-1, -1, 1, -1, 1, 1, -1, 1], 2))
    geometry.setIndex([0, 1, 2, 0, 2, 3])
    const material = new THREE.ShaderMaterial({
      name: 'BisStageBackdrop',
      vertexShader: VERT,
      fragmentShader: FRAG,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
      uniforms: {
        uCanvas: { value: new THREE.Vector2(1, 1) },
        uStage: { value: new THREE.Vector4(0, 0, 0, 0) },
        uFrame: { value: new THREE.Vector4(0, 0, 1, 1) },
        uBase: { value: rgb01(spec.base) },
        uGlow: { value: new THREE.Vector4(spec.glow.rgb[0] / 255, spec.glow.rgb[1] / 255, spec.glow.rgb[2] / 255, spec.glow.alpha) },
        uGlowBox: { value: new THREE.Vector4(spec.glow.cx, spec.glow.cy, spec.glow.rx, spec.glow.ry) },
        uGlowStop: { value: spec.glow.stop },
        uDots: { value: new THREE.Vector4(spec.dots.spacing, spec.dots.alpha, spec.dots.inner, spec.dots.outer) },
        uCheckA: { value: rgb01(spec.checker.a) },
        uCheckB: { value: rgb01(spec.checker.b) },
        uCheck: { value: new THREE.Vector2(spec.checker.cell, spec.checker.opacity) },
        bisToneMode: { value: 1 },
        bisSaturation: { value: 0 },
      },
    })
    const m = new THREE.Mesh(geometry, material)
    m.name = 'bis-stage-backdrop'
    m.frustumCulled = false
    m.renderOrder = -1e9 // first in the opaque list; no depth, so the icon always draws over it
    m.matrixAutoUpdate = false
    m.raycast = () => {}
    return m
  }, [])
  useEffect(
    () => () => {
      mesh.geometry.dispose()
      ;(mesh.material as THREE.Material).dispose()
    },
    [mesh],
  )

  const u = (mesh.material as THREE.ShaderMaterial).uniforms
  useLayoutEffect(() => {
    u.bisToneMode.value = display.mode
    u.bisSaturation.value = display.saturation
    invalidate()
  }, [u, display.mode, display.saturation, invalidate])

  // The checker belongs to the icon frame (like the Render view's CSS overlay): the editor's view window when it
  // zooms / pans (store.frame), else the whole canvas. Checked before each render.
  const store = useViewportStore()
  useFrame(() => {
    const c = u.uCanvas.value as THREE.Vector2
    const vf = store.frame ? resolveFrame(store.frame, c.x, c.y) : null
    const f = u.uFrame.value as THREE.Vector4
    if (vf) {
      if (f.x !== vf.x || f.y !== vf.y || f.z !== vf.side || f.w !== vf.side) f.set(vf.x, vf.y, vf.side, vf.side)
    } else if (f.x !== 0 || f.y !== 0 || f.z !== c.x || f.w !== c.y) f.set(0, 0, c.x, c.y)
  })

  // Locate the stage box relative to the canvas; it moves when the stage scrolls (zoomed in) or resizes.
  useLayoutEffect(() => {
    const canvas = gl.domElement
    let raf = 0
    const update = () => {
      raf = 0
      const cr = canvas.getBoundingClientRect()
      const sr = stage?.()?.getBoundingClientRect()
      const cw = Math.max(1, cr.width || size.width)
      const ch = Math.max(1, cr.height || size.height)
      const next = sr && sr.width > 0 ? [sr.left - cr.left, sr.top - cr.top, sr.width, sr.height] : [0, 0, 0, 0]
      const s = u.uStage.value as THREE.Vector4
      const c = u.uCanvas.value as THREE.Vector2
      if (c.x === cw && c.y === ch && s.x === next[0] && s.y === next[1] && s.z === next[2] && s.w === next[3]) return
      c.set(cw, ch)
      s.set(next[0], next[1], next[2], next[3])
      invalidate()
    }
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(update)
    }
    update()
    const el = stage?.()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(schedule) : null
    ro?.observe(canvas)
    if (el) ro?.observe(el)
    window.addEventListener('resize', schedule)
    document.addEventListener('scroll', schedule, { capture: true, passive: true })
    return () => {
      if (raf) cancelAnimationFrame(raf)
      ro?.disconnect()
      window.removeEventListener('resize', schedule)
      document.removeEventListener('scroll', schedule, { capture: true })
    }
  }, [gl, stage, u, size.width, size.height, invalidate])

  return <primitive object={mesh} />
}
