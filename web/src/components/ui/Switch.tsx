import { cn } from '../../lib/format'

export function Switch({
  checked,
  onChange,
  disabled,
  label,
  size = 'sm',
}: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
  label?: string
  size?: 'xs' | 'sm'
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex shrink-0 items-center rounded-full border transition-[background-color,border-color] duration-200 disabled:opacity-40',
        size === 'sm' ? 'h-[18px] w-[30px]' : 'h-[14px] w-[24px]',
        checked ? 'border-transparent accent-gradient' : 'border-line-2 bg-surface-0 hover:border-line-3',
      )}
    >
      <span
        className={cn(
          'absolute rounded-full bg-white shadow-[0_1px_3px_rgb(0_0_0/0.45)] transition-transform duration-200 ease-[var(--ease-spring)]',
          size === 'sm' ? 'left-[2px] h-3 w-3' : 'left-[2px] h-2 w-2',
          checked ? (size === 'sm' ? 'translate-x-[12px]' : 'translate-x-[10px]') : 'translate-x-0 opacity-80',
        )}
      />
    </button>
  )
}
