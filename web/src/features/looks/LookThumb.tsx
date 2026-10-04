// Composed look thumbnail: lighting backdrop · plate chip (plate material / fill) · the layer material as a
// rendered ball (cropped from B's Cycles swatch) with its shadow (physical or none) · a glint where the key light sits.
// `className` positions/sizes the thumbnail (e.g. 'absolute inset-0'); default 'relative'.
import { memo, useState } from 'react'
import type { Look, Presets } from '../../types'
import { cn } from '../../lib/format'
import { fillToCss } from '../../lib/color'
import { lightingCss, MATERIAL_CSS, PLATE_FINISH_CSS, PLATE_SHEEN_CSS } from '../../lib/meta'
import { lookHeroMaterial } from '../../lib/looks'
import { squirclePath } from '../../components/icons'

const SQUIRCLE_MASK = `url("data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><path d='${squirclePath(100, 0)}'/></svg>`,
)}")`

export const LookThumb = memo(function LookThumb({ look, presets, className }: { look: Look; presets: Presets | null; className?: string }) {
  const [swatchFailed, setSwatchFailed] = useState(false)
  const st = look.style
  const mat = lookHeroMaterial(look)
  const matId = mat?.preset ?? 'liquid_glass'
  const swatch = presets?.materials[matId]?.swatch
  const plateId = st.plate?.material?.preset ?? 'satin'
  const plateFill = st.plate?.fill
  const shape = st.plate?.shape ?? 'squircle'
  const lightId = st.lighting?.preset ?? 'studio'
  const lp = presets?.lighting[lightId]
  const angle = lp?.lockAngle ?? st.lighting?.angle ?? -45
  const shadow = st.layerDefaults?.shadow
  // Shadows are real (Cycles) or off — legacy neutral / chromatic kinds render as physical (PLAN §11).
  const castsShadow = !shadow || shadow.kind !== 'none'
  const plateBg = plateFill
    ? [PLATE_SHEEN_CSS[plateId], fillToCss(plateFill)].filter(Boolean).join(', ')
    : (PLATE_FINISH_CSS[plateId] ?? PLATE_FINISH_CSS.satin)
  const rad = (angle * Math.PI) / 180
  const glint = { left: `${50 + Math.sin(rad) * 44}%`, top: `${50 - Math.cos(rad) * 44}%` }
  const depth = Math.min(1, (st.layerDefaults?.depth?.thickness ?? 0.1) / 0.12)

  return (
    <span className={cn('block overflow-hidden', className ?? 'relative')} style={{ background: lightingCss(lightId, lp) }}>
      {/* key light: a soft glow on the backdrop where the light comes from */}
      <span
        className="pointer-events-none absolute h-[26%] w-[26%] -translate-x-1/2 -translate-y-1/2 rounded-full"
        style={{
          ...glint,
          background: `radial-gradient(circle, ${lp?.warmth ? '#ffe2b8' : (lp?.rimColors?.[0] ?? '#ffffff')} 0 12%, ${lp?.warmth ? 'rgb(255 190 120 / 0.45)' : 'rgb(255 255 255 / 0.4)'} 24%, transparent 66%)`,
        }}
      />
      {/* square stage (keeps the plate and ball round inside wide cards) */}
      <span className="absolute inset-y-0 left-1/2 aspect-square h-full -translate-x-1/2">
        {/* plate chip */}
        <span
          className="absolute inset-[16%] shadow-[0_6px_14px_-4px_rgb(0_0_0/0.55)]"
          style={{
            background: plateBg,
            borderRadius: shape === 'circle' ? '50%' : shape === 'rounded' ? '24%' : shape === 'square' ? '6%' : undefined,
            WebkitMaskImage: shape === 'squircle' ? SQUIRCLE_MASK : undefined,
            maskImage: shape === 'squircle' ? SQUIRCLE_MASK : undefined,
            WebkitMaskSize: '100% 100%',
            maskSize: '100% 100%',
          }}
        />
        {/* layer shadow */}
        {castsShadow && (
          <span
            className="absolute left-[31%] top-[46%] h-[28%] w-[38%] rounded-[50%] blur-[4px]"
            style={{
              background: 'rgba(0,0,0,.75)',
              opacity: 0.6,
              // Cast away from the key light (0° = light from the top, clockwise).
              transform: `translate(${-Math.sin(rad) * 12}%, ${10 + Math.cos(rad) * 12}%)`,
            }}
          />
        )}
        {/* layer material ball */}
        <span
          className="absolute left-[29%] top-[29%] h-[42%] w-[42%] overflow-hidden rounded-full"
          style={{
            background: MATERIAL_CSS[matId] ?? 'linear-gradient(135deg,#777,#333)',
            boxShadow: `0 ${1 + depth * 2}px ${2 + depth * 4}px rgb(0 0 0 / 0.35)`,
          }}
        >
          {swatch && !swatchFailed && (
            <img
              src={swatch}
              alt=""
              draggable={false}
              loading="lazy"
              onError={() => setSwatchFailed(true)}
              // The swatch is a full icon render with the material on a centred sphere (r ≈ 26 %): crop to it.
              className="absolute left-1/2 top-1/2 h-[206%] w-[206%] max-w-none object-cover"
              style={{ transform: 'translate(-49.4%, -49.4%)' }}
            />
          )}
        </span>
      </span>
    </span>
  )
})
