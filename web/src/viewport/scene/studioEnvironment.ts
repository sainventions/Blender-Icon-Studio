// Procedural studio environment built only from emissive "lightformers" (no network HDRIs) — the three.js twin of
// the worker's world shader + area-light rig: a vertical gradient dome with a front-fill term, a key softbox, a
// grazing rim strip on the lit side, a weaker opposite strip, a cool fill card and two faint side kickers.
//
// The environment is rendered for light angle 0 (light from the top) into a 256² cube target once per parameter
// change; the light angle itself is applied as scene.environmentRotation, so dragging the light dial costs nothing.
import * as THREE from 'three'
import { COOL, WARM, lightDir, type Rig } from './rig'

const DOME_VERT = /* glsl */ `
varying vec3 vDir;
void main() {
  vDir = normalize( position );
  gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
}`

const DOME_FRAG = /* glsl */ `
uniform vec3 c0; uniform vec3 c1; uniform vec3 c2; uniform vec3 c3;
uniform vec3 tint; uniform float strength; uniform float front;
varying vec3 vDir;
void main() {
  vec3 d = normalize( vDir );
  float t = d.y * 0.5 + 0.5;
  vec3 g = t < 0.55 ? mix( c0, c1, t / 0.55 ) : ( t < 0.8 ? mix( c1, c2, ( t - 0.55 ) / 0.25 ) : mix( c2, c3, ( t - 0.8 ) / 0.2 ) );
  float f = max( d.z, 0.0 ) * front;
  gl_FragColor = vec4( ( g + f ) * tint * strength, 1.0 );
}`

const CARD_VERT = /* glsl */ `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
}`

const CARD_FRAG = /* glsl */ `
uniform vec3 color; uniform vec2 soft; uniform float round;
varying vec2 vUv;
void main() {
  vec2 d = abs( vUv - 0.5 ) * 2.0;
  float a;
  if ( round > 0.5 ) {
    a = 1.0 - smoothstep( 1.0 - soft.x, 1.0, length( d ) );
  } else {
    a = ( 1.0 - smoothstep( 1.0 - soft.x, 1.0, d.x ) ) * ( 1.0 - smoothstep( 1.0 - soft.y, 1.0, d.y ) );
  }
  gl_FragColor = vec4( color * a, 1.0 );
}`

interface Card {
  mesh: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>
  set(intensity: number, color: THREE.Color): void
}

function makeCard(w: number, h: number, soft: [number, number], round: boolean): Card {
  const mat = new THREE.ShaderMaterial({
    vertexShader: CARD_VERT,
    fragmentShader: CARD_FRAG,
    uniforms: {
      color: { value: new THREE.Color() },
      soft: { value: new THREE.Vector2(soft[0], soft[1]) },
      round: { value: round ? 1 : 0 },
    },
    side: THREE.DoubleSide,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    depthTest: false,
    toneMapped: false,
  })
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat)
  mesh.renderOrder = 1
  return {
    mesh,
    set(intensity, color) {
      mat.uniforms.color.value.copy(color).multiplyScalar(Math.max(0, intensity))
      mesh.visible = intensity > 1e-4
    },
  }
}

function place(obj: THREE.Object3D, dir: THREE.Vector3, distance: number, up: THREE.Vector3): void {
  obj.position.copy(dir).multiplyScalar(distance)
  obj.up.copy(up)
  obj.lookAt(0, 0, 0)
}

export interface StudioEnvParams {
  rig: Rig
}

export class StudioEnvironment {
  readonly scene = new THREE.Scene()
  readonly target: THREE.WebGLCubeRenderTarget
  private readonly camera: THREE.CubeCamera
  private readonly dome: THREE.Mesh<THREE.SphereGeometry, THREE.ShaderMaterial>
  private readonly softbox = makeCard(7.4, 7.4, [0.55, 0.55], true)
  private readonly rimTop = makeCard(13, 0.85, [0.35, 0.7], false)
  private readonly rimOpp = makeCard(13, 0.85, [0.35, 0.7], false)
  private readonly fillCard = makeCard(8.4, 8.4, [0.6, 0.6], false)
  private readonly kickL = makeCard(0.7, 9, [0.7, 0.35], false)
  private readonly kickR = makeCard(0.7, 9, [0.7, 0.35], false)

  constructor(resolution = 256) {
    this.target = new THREE.WebGLCubeRenderTarget(resolution, { type: THREE.HalfFloatType, generateMipmaps: false })
    this.camera = new THREE.CubeCamera(0.1, 100, this.target)
    this.dome = new THREE.Mesh(
      new THREE.SphereGeometry(40, 48, 24),
      new THREE.ShaderMaterial({
        vertexShader: DOME_VERT,
        fragmentShader: DOME_FRAG,
        uniforms: {
          c0: { value: new THREE.Color(0.02, 0.02, 0.025) },
          c1: { value: new THREE.Color(0.18, 0.18, 0.2) },
          c2: { value: new THREE.Color(0.6, 0.6, 0.62) },
          c3: { value: new THREE.Color(1, 1, 1) },
          tint: { value: new THREE.Color(1, 1, 1) },
          strength: { value: 0.6 },
          front: { value: 0.5 },
        },
        side: THREE.BackSide,
        depthWrite: false,
        toneMapped: false,
      }),
    )
    this.dome.renderOrder = 0
    this.scene.add(
      this.dome,
      this.softbox.mesh,
      this.rimTop.mesh,
      this.rimOpp.mesh,
      this.fillCard.mesh,
      this.kickL.mesh,
      this.kickR.mesh,
      this.camera,
    )
  }

  /** Rebuild the light cards for `rig` (angle-independent: the angle is applied as environmentRotation). */
  configure(rig: Rig): void {
    const white = new THREE.Color(1, 1, 1)
    const up = new THREE.Vector3(0, 1, 0)
    const zUp = new THREE.Vector3(0, 0, 1)
    const env = rig.environment
    const domeU = this.dome.material.uniforms
    domeU.strength.value = 0.75 * env
    domeU.front.value = 0.5 * Math.max(0.3, rig.fill)
    domeU.tint.value.copy(white).lerp(WARM.clone().lerp(white, 0.5), rig.warmth * 0.6)

    const keyColor = white.clone().lerp(WARM, rig.warmth)
    place(this.softbox.mesh, lightDir(0, rig.elevation), 10, zUp)
    this.softbox.set(6 * Math.max(0.2, rig.key) * (0.55 + 0.45 * Math.min(1.5, env)), keyColor)

    const rimA = rig.rimColors[0] ? new THREE.Color(rig.rimColors[0]) : white
    const rimB = rig.rimColors[1] ? new THREE.Color(rig.rimColors[1]) : rimA
    const rimScale = rig.rim * Math.max(0.4, rig.intensity)
    place(this.rimTop.mesh, lightDir(0, 82), 10, zUp)
    this.rimTop.set(30 * rimScale, rimA)
    place(this.rimOpp.mesh, lightDir(180, 82), 10, zUp)
    this.rimOpp.set(8 * rimScale, rimB)

    place(this.fillCard.mesh, lightDir(160, 55), 10, zUp)
    this.fillCard.set(0.85 * rig.fill, white.clone().lerp(COOL, rig.warmth))

    place(this.kickL.mesh, lightDir(-90, 80), 10, up)
    this.kickL.set(4 * rimScale, rimB)
    place(this.kickR.mesh, lightDir(90, 80), 10, up)
    this.kickR.set(4 * rimScale, rimA)
  }

  render(gl: THREE.WebGLRenderer): void {
    const autoClear = gl.autoClear
    gl.autoClear = true
    this.camera.update(gl, this.scene)
    gl.autoClear = autoClear
  }

  dispose(): void {
    this.target.dispose()
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh
      if (m.isMesh) {
        m.geometry.dispose()
        ;(m.material as THREE.Material).dispose()
      }
    })
  }
}
