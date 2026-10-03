// Static UI metadata: strategies, export targets, animation kinds, appearance/material visuals.
import type { AnimateRequest, AppearanceId, ExportTarget, SplitStrategy } from '../types'

export const STRATEGIES: { id: SplitStrategy; label: string; description: string }[] = [
  { id: 'smart', label: 'Smart', description: 'Detects the plate, then groups shapes by stacking order and colour. Best default.' },
  { id: 'group', label: 'By group', description: 'One layer per top-level SVG group (<g>).' },
  { id: 'color', label: 'By colour', description: 'One layer per distinct fill colour.' },
  { id: 'element', label: 'Per element', description: 'Every shape becomes its own layer.' },
  { id: 'single', label: 'Single layer', description: 'All artwork in one layer above the plate.' },
]

export const EXPORT_TARGETS: { id: ExportTarget; label: string; description: string; badge?: string }[] = [
  { id: 'ios', label: 'iOS / iPadOS', description: 'AppIcon.appiconset — 1024 master with dark & tinted variants.' },
  { id: 'macos', label: 'macOS', description: '.icns + .iconset with Tahoe margins and baked shadow.' },
  { id: 'watchos', label: 'watchOS', description: '1088 px circular master.' },
  { id: 'android', label: 'Android', description: 'Adaptive foreground / background / monochrome + legacy mipmaps.' },
  { id: 'windows', label: 'Windows', description: 'Multi-size .ico (16 – 256 px).' },
  { id: 'web', label: 'Web / PWA', description: 'Favicons, apple-touch-icon, maskable PWA icons + manifest snippet.' },
  { id: 'marketing', label: 'Marketing', description: 'High-resolution hero PNGs for stores and landing pages.' },
  { id: 'icon', label: 'Apple .icon', description: 'Icon Composer bundle (icon.json + layer assets).', badge: 'Beta' },
  { id: 'blend', label: 'Blender scene', description: 'Packed .blend with geometry, materials and lights.' },
]

export const ANIMATION_KINDS: { id: AnimateRequest['kind']; label: string; description: string }[] = [
  { id: 'tilt', label: 'Tilt', description: 'Gentle gyro-style parallax wobble.' },
  { id: 'turntable', label: 'Turntable', description: 'Full 360° rotation of the layer stack.' },
  { id: 'float', label: 'Float', description: 'Layers bob softly in depth.' },
  { id: 'light-sweep', label: 'Light sweep', description: 'The key light orbits — highlights race around rims.' },
  { id: 'explode', label: 'Explode', description: 'Layers separate in depth and come back together.' },
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
