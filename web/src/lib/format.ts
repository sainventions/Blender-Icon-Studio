// Small formatting helpers shared across the UI.
export { clsx as cn } from 'clsx'

export function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v
}

export function round(v: number, decimals = 3): number {
  const k = 10 ** decimals
  return Math.round(v * k) / k
}

/** Number of decimals implied by a slider step (0.01 → 2, 0.005 → 3, 10 → 0). */
export function decimalsForStep(step: number | undefined): number {
  if (!step || step >= 1) return 0
  const s = String(step)
  if (s.includes('e-')) return Number(s.split('e-')[1])
  const dot = s.indexOf('.')
  return dot < 0 ? 0 : Math.min(4, s.length - dot - 1)
}

export function formatNumber(v: number, decimals = 2): string {
  if (!Number.isFinite(v)) return '—'
  const fixed = v.toFixed(decimals)
  // trim trailing zeros but keep at least one decimal when decimals > 0 for visual stability
  if (decimals <= 1) return fixed
  return fixed.replace(/(\.\d*?[1-9])0+$/, '$1').replace(/\.0+$/, '.0')
}

export function formatSeconds(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '—'
  if (s < 1) return `${Math.round(s * 1000)} ms`
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`
  const m = Math.floor(s / 60)
  const r = Math.round(s % 60)
  return `${m}m ${r.toString().padStart(2, '0')}s`
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, ms) / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`
}

export function formatMB(mb: number | null | undefined): string {
  if (mb == null) return '—'
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const d = Math.round((now - t) / 1000)
  if (d < 10) return 'just now'
  if (d < 60) return `${d}s ago`
  const m = Math.round(d / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h} h ago`
  const days = Math.round(h / 24)
  if (days < 7) return `${days} d ago`
  return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** Shorten "NVIDIA GeForce RTX 3070 Ti" → "RTX 3070 Ti". */
export function shortGpuName(name: string | undefined): string {
  if (!name) return 'GPU'
  return name.replace(/^NVIDIA\s+/i, '').replace(/^GeForce\s+/i, '').replace(/\s+Laptop GPU$/i, ' Laptop')
}

export function titleCase(s: string): string {
  return s.replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function isMac(): boolean {
  return typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
}

/** Display a shortcut string like "Mod+Shift+Z" with platform-appropriate glyphs. */
export function prettyShortcut(s: string): string {
  const mac = isMac()
  return s
    .split('+')
    .map((k) => {
      switch (k) {
        case 'Mod':
          return mac ? '⌘' : 'Ctrl'
        case 'Shift':
          return mac ? '⇧' : 'Shift'
        case 'Alt':
          return mac ? '⌥' : 'Alt'
        case 'Del':
          return mac ? '⌫' : 'Del'
        case 'Up':
          return '↑'
        case 'Down':
          return '↓'
        case 'Left':
          return '←'
        case 'Right':
          return '→'
        default:
          return k
      }
    })
    .join(mac ? '' : '+')
}

/** Stable short hash of a string (FNV-1a, base36). */
export function hashString(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(36)
}
