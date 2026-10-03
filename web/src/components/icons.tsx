// Domain icons: app logo, platform / export-target glyphs, appearance glyphs, plate shapes.
import { useId, type SVGProps } from 'react'
import {
  AppWindow,
  Box,
  Circle,
  Contrast,
  Globe,
  Layers,
  Megaphone,
  Monitor,
  Moon,
  Package,
  Palette,
  Smartphone,
  Square,
  SquareDashed,
  Sun,
  Watch,
} from 'lucide-react'
import type { AppearanceId, ExportTarget, Platform, PlateShape } from '../types'

/** Superellipse |x|^n + |y|^n = 1 as an SVG path inside a box of `size` (with `pad`). */
export function squirclePath(size: number, pad = 0, n = 5, segments = 96): string {
  const r = size / 2 - pad
  const c = size / 2
  const pts: string[] = []
  for (let i = 0; i < segments; i++) {
    const t = (i / segments) * Math.PI * 2
    const ct = Math.cos(t)
    const st = Math.sin(t)
    const x = c + Math.sign(ct) * Math.abs(ct) ** (2 / n) * r
    const y = c + Math.sign(st) * Math.abs(st) ** (2 / n) * r
    pts.push(`${x.toFixed(2)},${y.toFixed(2)}`)
  }
  return `M${pts.join('L')}Z`
}

export function Logo({ size = 22, className }: { size?: number; className?: string }) {
  const id = useId().replace(/:/g, '')
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" className={className} aria-hidden>
      <defs>
        <linearGradient id={`lg-a${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#a594ff" />
          <stop offset="0.55" stopColor="#6f7dff" />
          <stop offset="1" stopColor="#2fd4f0" />
        </linearGradient>
        <linearGradient id={`lg-b${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fff" stopOpacity="0.95" />
          <stop offset="1" stopColor="#fff" stopOpacity="0.35" />
        </linearGradient>
        <radialGradient id={`lg-c${id}`} cx="0.35" cy="0.3" r="0.8">
          <stop offset="0" stopColor="#fff" stopOpacity="0.55" />
          <stop offset="1" stopColor="#fff" stopOpacity="0.05" />
        </radialGradient>
      </defs>
      <path d={squirclePath(64, 1)} fill={`url(#lg-a${id})`} />
      <path d={squirclePath(64, 1)} fill="none" stroke="#fff" strokeOpacity="0.35" strokeWidth="1.2" />
      <rect x="15" y="22" width="34" height="26" rx="13" fill={`url(#lg-c${id})`} stroke={`url(#lg-b${id})`} strokeWidth="1.6" />
      <circle cx="32" cy="23" r="9.5" fill="#fff" fillOpacity="0.22" stroke={`url(#lg-b${id})`} strokeWidth="1.6" />
      <ellipse cx="28.5" cy="19.5" rx="3.2" ry="1.8" fill="#fff" fillOpacity="0.85" />
    </svg>
  )
}

function AndroidGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" {...props}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="4.5" strokeDasharray="2.5 2" />
    </svg>
  )
}

function WindowsGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinejoin="round" {...props}>
      <rect x="3.5" y="3.5" width="7.5" height="7.5" rx="1.2" />
      <rect x="13" y="3.5" width="7.5" height="7.5" rx="1.2" />
      <rect x="3.5" y="13" width="7.5" height="7.5" rx="1.2" />
      <rect x="13" y="13" width="7.5" height="7.5" rx="1.2" />
    </svg>
  )
}

export function SquircleGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} {...props}>
      <path d={squirclePath(24, 3.2)} />
    </svg>
  )
}

function RoundedGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} {...props}>
      <rect x="3.5" y="3.5" width="17" height="17" rx="5" />
    </svg>
  )
}

export function PlatformIcon({ platform, className }: { platform: Platform | ExportTarget; className?: string }) {
  const c = className ?? 'h-4 w-4'
  switch (platform) {
    case 'ios':
      return <Smartphone className={c} />
    case 'macos':
      return <Monitor className={c} />
    case 'watchos':
      return <Watch className={c} />
    case 'android':
      return <AndroidGlyph className={c} />
    case 'windows':
      return <WindowsGlyph className={c} />
    case 'web':
      return <Globe className={c} />
    case 'marketing':
      return <Megaphone className={c} />
    case 'icon':
      return <Package className={c} />
    case 'blend':
      return <Box className={c} />
    case 'free':
      return <AppWindow className={c} />
    default:
      return <Layers className={c} />
  }
}

export function ShapeIcon({ shape, className }: { shape: PlateShape; className?: string }) {
  const c = className ?? 'h-3.5 w-3.5'
  switch (shape) {
    case 'squircle':
      return <SquircleGlyph className={c} />
    case 'circle':
      return <Circle className={c} />
    case 'rounded':
      return <RoundedGlyph className={c} />
    case 'square':
      return <Square className={c} />
    case 'none':
      return <SquareDashed className={c} />
  }
}

export function AppearanceIcon({ appearance, className }: { appearance: AppearanceId; className?: string }) {
  const c = className ?? 'h-3.5 w-3.5'
  switch (appearance) {
    case 'light':
      return <Sun className={c} />
    case 'dark':
      return <Moon className={c} />
    case 'clear-light':
    case 'clear-dark':
      return <Contrast className={c} />
    case 'tinted-light':
    case 'tinted-dark':
      return <Palette className={c} />
  }
}
