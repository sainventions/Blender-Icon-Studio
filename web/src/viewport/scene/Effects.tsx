// Post-processing: neon bloom (selective — like the worker's Glare on the Emission pass only, so a bright white plate
// never glows) + a faint highlight bloom → tone mapping matching the project's colour mode ('brand' = Standard + the
// worker's highlight soft clip by default, round 5; Khronos PBR Neutral, AgX) → selection + hover outlines → SMAA.
import { use, useEffect, useMemo, useSyncExternalStore } from 'react'
import {
  Bloom,
  EffectComposer,
  EffectComposerContext,
  HueSaturation,
  Outline,
  SMAA,
  ToneMapping,
  useSelectionSync,
} from '@react-three/postprocessing'
import { BlendFunction, KernelSize, SelectiveBloomEffect, ToneMappingMode } from 'postprocessing'
import { HalfFloatType, type Object3D } from 'three'
import type { RenderSettings } from '../../types'
import { AGX_PUNCHY_SATURATION, colorModeId } from './displayTransform'
import { SoftClipEffect } from './softClipEffect'
import { useViewportStore } from './store'

interface Props {
  colorMode: RenderSettings['colorMode'] | null | undefined
  selectedId: string | null
  /** 0..1 neon bloom request (max over visible neon layers). */
  bloom: number
  /** Layers whose meshes bloom (neon). */
  bloomIds: string[]
  multisampling: number
}

const SELECT_COLOR = 0x8f7dff
const SELECT_HIDDEN = 0x4a3f99
const HOVER_COLOR = 0xdfe6ff
const HOVER_HIDDEN = 0x6f7690
/** Object layers used by the selection effects (outlines use 10 / 11). */
const NEON_LAYER = 12

/**
 * Live neon bloom, calibrated against the worker's Glare (render.configure_compositor: threshold BLOOM_THRESHOLD 0.35,
 * strength 0.3 + 0.9 × bloom, size 0.2 + 0.3 × bloom) on 8 neon-look icons in Cycles previews: threshold 0.3 and
 * NEON_BLOOM_LIVE × the worker's strength curve (round-5 review: the round-3 0.15 / 0.4 + 1.2 × bloom laid a grey veil
 * over the dark plate, hidden by PBR Neutral's toe but plain under 'brand': plate +24 levels, ΔE 6.6 → 3.4 brand,
 * 7.3 → 4.5 neutral). The blur radius (postprocessing's mipmap spread, not Blender's size) keeps 0.45 + 0.4 × bloom.
 */
const NEON_BLOOM_THRESHOLD = 0.3
const NEON_BLOOM_LIVE = 0.25 / 0.3

function NeonBloom({ selection, strength }: { selection: Object3D[]; strength: number }) {
  const ctx = use(EffectComposerContext)
  const effect = useMemo(() => {
    const e = new SelectiveBloomEffect(ctx.scene, ctx.camera, {
      mipmapBlur: true,
      luminanceThreshold: NEON_BLOOM_THRESHOLD,
      luminanceSmoothing: 0.1,
      radius: 0.65,
      intensity: 1,
    })
    e.ignoreBackground = true
    return e
  }, [ctx.scene, ctx.camera])
  useEffect(() => {
    effect.intensity = NEON_BLOOM_LIVE * (0.3 + 0.9 * strength)
    effect.mipmapBlurPass.radius = Math.min(0.95, 0.45 + 0.4 * strength)
  }, [effect, strength])
  useEffect(() => () => effect.dispose(), [effect])
  useSelectionSync(effect, selection, NEON_LAYER)
  return <primitive object={effect} />
}

export function Effects({ colorMode: rawColorMode, selectedId, bloom, bloomIds, multisampling }: Props) {
  // Missing / unknown colour modes render as the worker's default (presets.DEFAULT_COLOR_MODE = 'brand').
  const colorMode = colorModeId(rawColorMode)
  const softClip = useMemo(() => new SoftClipEffect(), [])
  useEffect(() => () => softClip.dispose(), [softClip])
  const store = useViewportStore()
  const version = useSyncExternalStore(store.subscribe, store.getVersion)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const selection = useMemo(() => store.meshesOf(selectedId), [store, selectedId, version])
  const hoverId = store.dragging ? null : store.hovered
  const hover = useMemo(
    () => (hoverId && hoverId !== selectedId ? store.meshesOf(hoverId) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [store, hoverId, selectedId, version],
  )
  const bloomKey = bloomIds.join('|')
  const neon = useMemo(
    () => bloomIds.flatMap((id) => store.meshesOf(id)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [store, bloomKey, version],
  )
  const mode =
    colorMode === 'standard'
      ? ToneMappingMode.LINEAR
      : colorMode === 'agx' || colorMode === 'agx-punchy'
        ? ToneMappingMode.AGX
        : ToneMappingMode.NEUTRAL

  return (
    <EffectComposer multisampling={multisampling} autoClear={false} frameBufferType={HalfFloatType}>
      {bloom > 0 && neon.length > 0 && <NeonBloom selection={neon} strength={bloom} />}
      <Bloom mipmapBlur intensity={0.1} luminanceThreshold={6} luminanceSmoothing={0.25} radius={0.55} />
      {colorMode === 'brand' ? <primitive object={softClip} /> : <ToneMapping mode={mode} />}
      <HueSaturation
        saturation={colorMode === 'agx-punchy' ? AGX_PUNCHY_SATURATION : 0}
        blendFunction={colorMode === 'agx-punchy' ? BlendFunction.SRC : BlendFunction.SKIP}
      />
      <Outline
        selection={hover}
        selectionLayer={11}
        visibleEdgeColor={HOVER_COLOR}
        hiddenEdgeColor={HOVER_HIDDEN}
        edgeStrength={hover.length ? 1.6 : 0}
        blur
        kernelSize={KernelSize.VERY_SMALL}
        xRay
        blendFunction={hover.length ? BlendFunction.SCREEN : BlendFunction.SKIP}
      />
      <Outline
        selection={selection}
        selectionLayer={10}
        visibleEdgeColor={SELECT_COLOR}
        hiddenEdgeColor={SELECT_HIDDEN}
        edgeStrength={selection.length ? 6 : 0}
        blur
        kernelSize={KernelSize.VERY_SMALL}
        xRay
        blendFunction={selection.length ? BlendFunction.ALPHA : BlendFunction.SKIP}
      />
      <SMAA />
    </EffectComposer>
  )
}
