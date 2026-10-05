// Static UI metadata: strategies, export targets, animation kinds, appearance/material visuals.
import type { AnimateRequest, AppearanceId, ExportTarget, LightingPreset, SampleIcon, SplitStrategy } from '../types'

export const STRATEGIES: { id: SplitStrategy; label: string; description: string }[] = [
  { id: 'smart', label: 'Smart', description: 'Detects the plate, then groups shapes by stacking order and colour. Best default.' },
  { id: 'group', label: 'By group', description: 'One layer per top-level SVG group (<g>).' },
  { id: 'color', label: 'By colour', description: 'One layer per distinct fill colour.' },
  { id: 'element', label: 'Per element', description: 'Every shape becomes its own layer.' },
  { id: 'single', label: 'Single layer', description: 'All artwork in one layer above the plate.' },
]

export const EXPORT_TARGETS: { id: ExportTarget; label: string; description: string; badge?: string }[] = [
  { id: 'ios', label: 'iOS / iPadOS', description: 'AppIcon.appiconset: 1024 master with dark & tinted variants.' },
  { id: 'macos', label: 'macOS', description: '.icns + .iconset with Tahoe margins and baked shadow.' },
  { id: 'watchos', label: 'watchOS', description: '1088 px circular master.' },
  { id: 'android', label: 'Android', description: 'Adaptive foreground / background / monochrome + legacy mipmaps.' },
  { id: 'windows', label: 'Windows', description: 'Multi-size .ico (16-256 px).' },
  { id: 'web', label: 'Web / PWA', description: 'Favicons, apple-touch-icon, maskable PWA icons + manifest snippet.' },
  { id: 'marketing', label: 'Marketing', description: 'High-resolution hero PNGs for stores and landing pages.' },
  { id: 'icon', label: 'Apple .icon', description: 'Icon Composer bundle (icon.json + layer assets).', badge: 'Beta' },
  { id: 'blend', label: 'Blender scene', description: 'Packed .blend with geometry, materials and lights.' },
]

export const ANIMATION_KINDS: { id: AnimateRequest['kind']; label: string; description: string }[] = [
  { id: 'tilt', label: 'Tilt', description: 'Gentle gyro-style parallax wobble.' },
  { id: 'turntable', label: 'Turntable', description: 'Full 360° rotation of the layer stack.' },
  { id: 'float', label: 'Float', description: 'Layers bob softly in depth.' },
  { id: 'light-sweep', label: 'Light sweep', description: 'The key light orbits, so highlights race around rims.' },
  { id: 'iso', label: 'Iso sweep', description: 'The camera swings from head-on to isometric and back, showing the real layer distances like a CAD view.' },
]

/** Visual of each appearance for chips (background + foreground hint). */
export const APPEARANCE_VISUAL: Record<AppearanceId, { bg: string; fg: string; ring: string }> = {
  light: { bg: 'linear-gradient(180deg,#ffffff,#e4e5ea)', fg: '#2563eb', ring: '#ffffff' },
  dark: { bg: 'linear-gradient(180deg,#3a3a3f,#111114)', fg: '#60a5fa', ring: '#3a3a3f' },
  'clear-light': { bg: 'linear-gradient(135deg,#e0e7ff,#fce7f3 55%,#cffafe)', fg: 'rgba(255,255,255,.85)', ring: '#c7d2fe' },
  'clear-dark': { bg: 'linear-gradient(135deg,#0f172a,#1e1b4b 60%,#0c4a6e)', fg: 'rgba(255,255,255,.75)', ring: '#1e1b4b' },
  'tinted-light': { bg: 'linear-gradient(180deg,#dbeafe,#bfdbfe)', fg: '#1d4ed8', ring: '#bfdbfe' },
  'tinted-dark': { bg: 'linear-gradient(180deg,#1f2937,#0b0f19)', fg: '#3b82f6', ring: '#1f2937' },
}

/** Fallback CSS "swatches" for material presets (used until rendered swatch PNGs exist). */
export const MATERIAL_CSS: Record<string, string> = {
  liquid_glass:
    'radial-gradient(circle at 30% 25%, rgba(255,255,255,.95) 0 6%, transparent 22%), radial-gradient(circle at 70% 80%, rgba(147,197,253,.55), transparent 55%), linear-gradient(145deg, rgba(196,181,253,.55), rgba(103,232,249,.35))',
  clear_glass:
    'radial-gradient(circle at 28% 22%, #fff 0 5%, transparent 18%), linear-gradient(160deg, rgba(255,255,255,.35), rgba(186,230,253,.12) 60%, rgba(255,255,255,.3))',
  frosted_glass:
    'radial-gradient(circle at 30% 25%, rgba(255,255,255,.7), transparent 45%), linear-gradient(150deg, #e5e7eb, #9ca3af)',
  dispersive_crystal:
    'conic-gradient(from 210deg at 60% 60%, #f472b6, #facc15, #4ade80, #22d3ee, #818cf8, #f472b6), radial-gradient(circle at 30% 25%, #fff, transparent 40%)',
  tinted_glass: 'radial-gradient(circle at 30% 25%, rgba(255,255,255,.75), transparent 30%), linear-gradient(150deg, #be123c, #7c2d12)',
  glossy_plastic: 'radial-gradient(circle at 32% 26%, #fff 0 7%, transparent 26%), linear-gradient(150deg, #fb7185, #e11d48)',
  satin: 'radial-gradient(circle at 35% 30%, rgba(255,255,255,.45), transparent 55%), linear-gradient(150deg, #f4f4f5, #a1a1aa)',
  candy: 'radial-gradient(circle at 30% 25%, #fff 0 6%, transparent 24%), radial-gradient(circle at 60% 65%, #fda4af, #e11d48 75%)',
  gummy: 'radial-gradient(circle at 35% 30%, rgba(255,255,255,.6), transparent 35%), radial-gradient(circle at 55% 60%, #86efac, #16a34a 80%)',
  jelly: 'radial-gradient(circle at 30% 25%, #fff 0 5%, transparent 22%), radial-gradient(circle at 55% 60%, rgba(196,181,253,.9), rgba(109,40,217,.8) 80%)',
  chrome:
    'linear-gradient(180deg, #fafafa 0%, #a1a1aa 38%, #18181b 50%, #71717a 62%, #e4e4e7 100%)',
  brushed_metal:
    'repeating-conic-gradient(from 0deg, rgba(255,255,255,.08) 0deg 2deg, rgba(0,0,0,.06) 2deg 4deg), linear-gradient(150deg, #93c5fd, #1e40af)',
  matte_clay: 'radial-gradient(circle at 35% 30%, #fde68a, #d97706 85%)',
  iridescent: 'conic-gradient(from 120deg, #a78bfa, #22d3ee, #86efac, #fde047, #f472b6, #a78bfa)',
  neon: 'radial-gradient(circle, #f0abfc 0 18%, #c026d3 40%, rgba(192,38,211,.15) 70%, transparent), #0a0a0f',
  flat: 'linear-gradient(135deg, #8f7dff 50%, #2fd4f0 50%)',
}

export const CATEGORY_ORDER = ['Glass', 'Solid', 'Metal', 'Light']

/** Little visual for a lighting preset (chips, look thumbnails): a dark studio with the key light's warmth. */
export function lightingCss(id: string, p: LightingPreset | undefined): string {
  if (!p) return 'radial-gradient(circle at 25% 20%, rgba(255,255,255,0.55), transparent 55%), #2a2a33'
  if (p.rimColors?.length) return `radial-gradient(circle at 30% 30%, ${p.rimColors[0]}, transparent 60%), radial-gradient(circle at 75% 75%, ${p.rimColors[1] ?? p.rimColors[0]}, transparent 60%), #0b0b12`
  const warm = p.warmth > 0 ? `rgba(255,170,90,${0.25 + p.warmth * 0.4})` : 'rgba(255,255,255,0.75)'
  const env = Math.min(1, p.environment / 1.8)
  const base = `rgb(${Math.round(30 + env * 120)} ${Math.round(30 + env * 120)} ${Math.round(38 + env * 120)})`
  if (id === 'top' || p.lockAngle === 0) return `radial-gradient(ellipse 80% 55% at 50% 0%, ${warm}, transparent 70%), ${base}`
  return `radial-gradient(circle at 25% 20%, ${warm}, transparent ${40 + p.key * 15}%), linear-gradient(135deg, transparent 60%, rgba(255,255,255,${Math.min(0.6, p.rim * 0.25)})), ${base}`
}

/** Neutral plate chips per plate material (the plate's colour comes from each icon's own fill). */
export const PLATE_FINISH_CSS: Record<string, string> = {
  satin: 'radial-gradient(ellipse 70% 50% at 32% 18%, rgba(255,255,255,.55), transparent 70%), linear-gradient(180deg, #fbfbfc, #d7d8df)',
  glossy_plastic:
    'radial-gradient(ellipse 55% 30% at 36% 14%, rgba(255,255,255,.98), transparent 72%), linear-gradient(180deg, #ffffff 0%, #e9eaf0 55%, #c9cbd4 100%)',
  matte_clay: 'linear-gradient(180deg, #efe8df, #cfc4b6)',
  brushed_metal:
    'repeating-linear-gradient(90deg, rgba(255,255,255,.07) 0 1px, rgba(0,0,0,.05) 1px 3px), linear-gradient(180deg, #e3e7ec 0%, #a9b1bc 55%, #7d8693 100%)',
  chrome: 'linear-gradient(180deg, #fafafa 0%, #b4b4bc 40%, #3f3f46 52%, #8a8a93 64%, #ececf0 100%)',
  frosted_glass: 'radial-gradient(circle at 30% 25%, rgba(255,255,255,.8), transparent 50%), linear-gradient(150deg, #eef0f4, #b9bdc7)',
}

/** Sheen laid over a custom plate fill so the finish still reads. */
export const PLATE_SHEEN_CSS: Record<string, string> = {
  glossy_plastic: 'radial-gradient(ellipse 55% 30% at 36% 14%, rgba(255,255,255,.55), transparent 72%)',
  satin: 'radial-gradient(ellipse 70% 50% at 32% 18%, rgba(255,255,255,.16), transparent 70%)',
  brushed_metal: 'repeating-linear-gradient(90deg, rgba(255,255,255,.05) 0 1px, rgba(0,0,0,.05) 1px 3px)',
  chrome: 'linear-gradient(180deg, rgba(255,255,255,.35), transparent 45%, rgba(255,255,255,.18))',
}

/** Sample collections (the server tags samples with their source folder). */
export const SAMPLE_COLLECTION_LABEL: Record<string, string> = { corpus: 'App icons', svgtests: 'Test SVGs' }

export function sampleCollection(s: SampleIcon): string {
  return s.collection ?? 'corpus'
}
