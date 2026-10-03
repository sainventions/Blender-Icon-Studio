// Post-processing: neon bloom (selective — like the worker's Glare on the Emission pass only, so a bright white plate
// never glows) + a faint highlight bloom → tone mapping matching the project's colour mode (Khronos PBR Neutral by
// default, D6) → selection + hover outlines → SMAA.
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
import { AGX_PUNCHY_SATURATION } from './displayTransform'
import { useViewportStore } from './store'

interface Props {
  colorMode: RenderSettings['colorMode']
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
 * Bloom restricted to the visible pixels of the neon meshes (depth-masked), with the worker's Glare settings:
 * threshold 0.15, strength 0.4 + 1.2 × bloom, size 0.45 + 0.4 × bloom.
 */
function NeonBloom({ selection, strength }: { selection: Object3D[]; strength: number }) {
  const ctx = use(EffectComposerContext)
  const effect = useMemo(() => {
    const e = new SelectiveBloomEffect(ctx.scene, ctx.camera, {
      mipmapBlur: true,
      luminanceThreshold: 0.15,
      luminanceSmoothing: 0.1,
      radius: 0.65,
      intensity: 1,
    })
    e.ignoreBackground = true
    return e
  }, [ctx.scene, ctx.camera])
  useEffect(() => {
    effect.intensity = 0.4 + 1.2 * strength
    effect.mipmapBlurPass.radius = Math.min(0.95, 0.45 + 0.4 * strength)
  }, [effect, strength])
  useEffect(() => () => effect.dispose(), [effect])
  useSelectionSync(effect, selection, NEON_LAYER)
  return <primitive object={effect} />
}

export function Effects({ colorMode, selectedId, bloom, bloomIds, multisampling }: Props) {
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
      <ToneMapping mode={mode} />
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
