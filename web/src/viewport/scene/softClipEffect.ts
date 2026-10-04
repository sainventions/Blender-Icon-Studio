// The 'brand' colour mode's display transform as a post-processing effect: blender_worker's compositor highlight soft
// clip (render.configure_compositor / util.soft_clip) followed by the Standard view transform (the composer's sRGB
// output encoding clamps per channel, like Blender's Standard). Per channel, scene-linear:
//   y = x                                      x ≤ k
//   y = 1 − (1 − k) · exp(−(x − k) / (1 − k))  x > k
// so every paint below the knee displays exactly (materials3d pre-compensates paints with the exact inverse) while
// rims and glints roll off smoothly toward white. Takes the place of the ToneMapping effect in Effects.tsx.
import { BlendFunction, Effect } from 'postprocessing'
import { Uniform } from 'three'
import { BRAND_KNEE } from './displayTransform'

const FRAGMENT = /* glsl */ `
uniform float knee;
void mainImage(const in vec4 inputColor, const in vec2 uv, out vec4 outputColor) {
  vec3 x = max(inputColor.rgb, vec3(0.0));
  float w = max(1.0 - knee, 1e-4);
  vec3 hi = 1.0 - w * exp(-(x - knee) / w);
  outputColor = vec4(mix(x, hi, step(vec3(knee), x)), inputColor.a);
}
`

export class SoftClipEffect extends Effect {
  constructor(knee: number = BRAND_KNEE) {
    super('SoftClipEffect', FRAGMENT, {
      blendFunction: BlendFunction.SRC,
      uniforms: new Map<string, Uniform>([['knee', new Uniform(knee)]]),
    })
  }

  get knee(): number {
    return (this.uniforms.get('knee') as Uniform<number>).value
  }

  set knee(v: number) {
    ;(this.uniforms.get('knee') as Uniform<number>).value = v
  }
}
