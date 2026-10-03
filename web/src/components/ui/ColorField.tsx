// Colour swatch + hex field with a react-colorful popover (hex input, alpha, eyedropper, swatches, recents).
import { useEffect, useRef, useState } from 'react'
import { HexColorInput, HexColorPicker } from 'react-colorful'
import { Pipette } from 'lucide-react'
import { cn } from '../../lib/format'
import { normalizeHex, rgba, SYSTEM_SWATCHES } from '../../lib/color'
import { safeStorage } from '../../lib/hooks'
import { Popover } from './Popover'
import { NumberField, type ChangePhase } from './NumberField'

const RECENT_KEY = 'bis.recentColors'
function loadRecent(): string[] {
  try {
    const v = JSON.parse(safeStorage.get(RECENT_KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, 12) : []
  } catch {
    return []
  }
}
function pushRecent(hex: string) {
  const list = [hex, ...loadRecent().filter((c) => c !== hex)].slice(0, 12)
  safeStorage.set(RECENT_KEY, JSON.stringify(list))
}

interface EyeDropperCtor {
  new (): { open: () => Promise<{ sRGBHex: string }> }
}

export interface ColorFieldProps {
  value: string
  onChange: (hex: string, phase: ChangePhase) => void
  alpha?: number
  onAlphaChange?: (a: number, phase: ChangePhase) => void
  disabled?: boolean
  className?: string
  /** Hide the inline hex text (swatch only). */
  swatchOnly?: boolean
  ariaLabel?: string
}

export function ColorField({ value, onChange, alpha, onAlphaChange, disabled, className, swatchOnly, ariaLabel }: ColorFieldProps) {
  const [open, setOpen] = useState(false)
  const anchorRef = useRef<HTMLButtonElement>(null)
  const hex = normalizeHex(value) ?? '#000000'
  return (
    <div className={cn('flex min-w-0 items-center gap-1.5', className)}>
      <button
        ref={anchorRef}
        type="button"
        disabled={disabled}
        aria-label={ariaLabel ?? 'Pick colour'}
        onClick={() => setOpen((o) => !o)}
        className={cn(
          'relative h-6 w-6 shrink-0 overflow-hidden rounded-md border border-line-2 checkerboard-sm shadow-[0_1px_0_rgb(255_255_255/0.08)_inset] transition-[box-shadow] hover:shadow-[0_0_0_2px_rgb(255_255_255/0.12)] disabled:opacity-45',
          open && 'shadow-[0_0_0_2px_rgb(143_125_255/0.6)]',
        )}
      >
        <span className="absolute inset-0" style={{ background: rgba(hex, alpha ?? 1) }} />
      </button>
      {!swatchOnly && (
        <label className="flex h-6 min-w-0 flex-1 items-center rounded-md border border-line bg-surface-0 pl-1.5 transition-colors focus-within:border-accent/70 hover:border-line-2">
          <span className="text-3xs text-fg-4">#</span>
          <HexColorInput
            color={hex}
            disabled={disabled}
            onChange={(v) => {
              const n = normalizeHex(v)
              if (n) onChange(n, 'commit')
            }}
            onKeyDown={(e) => e.stopPropagation()}
            className="h-full w-full min-w-0 bg-transparent px-1 font-mono text-2xs uppercase text-fg outline-none"
          />
          {alpha !== undefined && onAlphaChange && (
            <NumberField
              value={alpha}
              onChange={onAlphaChange}
              min={0}
              max={1}
              step={0.01}
              scale={100}
              decimals={0}
              unit="%"
              className="h-[22px] w-[50px] shrink-0 border-0 border-l border-line bg-transparent rounded-none rounded-r-md"
            />
          )}
        </label>
      )}
      <Popover open={open} onClose={() => setOpen(false)} anchor={anchorRef.current} placement="left-start" className="w-[232px] p-2.5">
        <ColorPanel
          value={hex}
          alpha={alpha}
          onChange={onChange}
          onAlphaChange={onAlphaChange}
        />
      </Popover>
    </div>
  )
}

export function ColorPanel({
  value,
  onChange,
  alpha,
  onAlphaChange,
}: {
  value: string
  onChange: (hex: string, phase: ChangePhase) => void
  alpha?: number
  onAlphaChange?: (a: number, phase: ChangePhase) => void
}) {
  const [recent, setRecent] = useState<string[]>(() => loadRecent())
  const latest = useRef(value)
  latest.current = value
  const changedRef = useRef(false)
  const EyeDropper = (window as unknown as { EyeDropper?: EyeDropperCtor }).EyeDropper

  // Remember the colour when the panel closes after a change.
  useEffect(
    () => () => {
      if (changedRef.current) pushRecent(latest.current)
    },
    [],
  )

  const pick = (hex: string) => {
    changedRef.current = true
    onChange(hex, 'commit')
    pushRecent(hex)
    setRecent(loadRecent())
  }

  return (
    <div className="space-y-2.5">
      <div
        className="bis-colorful"
        onPointerUp={() => {
          if (changedRef.current) onChange(latest.current, 'commit')
        }}
      >
        <HexColorPicker
          color={value}
          onChange={(v) => {
            changedRef.current = true
            onChange(v, 'change')
          }}
        />
      </div>
      <div className="flex items-center gap-1.5">
        {EyeDropper && (
          <button
            type="button"
            data-tip="Pick from screen"
            onClick={async () => {
              try {
                const r = await new EyeDropper().open()
                const n = normalizeHex(r.sRGBHex)
                if (n) pick(n)
              } catch {
                /* cancelled */
              }
            }}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-line bg-surface-0 text-fg-3 hover:border-line-2 hover:text-fg"
          >
            <Pipette className="h-3.5 w-3.5" />
          </button>
        )}
        <label className="flex h-6 min-w-0 flex-1 items-center rounded-md border border-line bg-surface-0 pl-1.5 focus-within:border-accent/70">
          <span className="text-3xs text-fg-4">#</span>
          <HexColorInput
            color={value}
            onChange={(v) => {
              const n = normalizeHex(v)
              if (n) pick(n)
            }}
            onKeyDown={(e) => e.stopPropagation()}
            className="h-full w-full min-w-0 bg-transparent px-1 font-mono text-2xs uppercase text-fg outline-none"
          />
        </label>
        {alpha !== undefined && onAlphaChange && (
          <NumberField value={alpha} onChange={onAlphaChange} min={0} max={1} step={0.01} scale={100} decimals={0} unit="%" className="w-[58px] shrink-0" />
        )}
      </div>
      <div className="grid grid-cols-9 gap-1">
        {SYSTEM_SWATCHES.map((c) => (
          <button
            key={c}
            type="button"
            data-tip={c}
            onClick={() => pick(c)}
            className={cn(
              'aspect-square rounded-[5px] border border-white/10 transition-transform hover:scale-110',
              c.toLowerCase() === value.toLowerCase() && 'ring-2 ring-accent ring-offset-1 ring-offset-surface-2',
            )}
            style={{ background: c }}
          />
        ))}
      </div>
      {recent.length > 0 && (
        <div>
          <div className="mb-1 text-3xs font-semibold uppercase tracking-[0.08em] text-fg-4">Recent</div>
          <div className="grid grid-cols-9 gap-1">
            {recent.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => pick(c)}
                className="aspect-square rounded-[5px] border border-white/10 transition-transform hover:scale-110"
                style={{ background: c }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
