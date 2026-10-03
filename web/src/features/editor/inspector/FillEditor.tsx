// Fill editor: Auto / None / Solid / Linear / Radial / System Light / System Dark.
import type { Fill, FillLinear, FillRadial } from '../../../types'
import { fillToCss, linearAngle, linearPointsFromAngle } from '../../../lib/color'
import { convertFill, type FillType } from '../../../lib/projectOps'
import { cn } from '../../../lib/format'
import { ColorField, GradientEditor, NumberField, Row, Select, type ChangePhase, type SelectOption } from '../../../components/ui'

const FILL_LABEL: Record<FillType, string> = {
  auto: 'Automatic (SVG)',
  none: 'None',
  solid: 'Solid colour',
  linear: 'Linear gradient',
  radial: 'Radial gradient',
  'system-light': 'System Light',
  'system-dark': 'System Dark',
}

const FILL_HELP: Partial<Record<FillType, string>> = {
  auto: 'Uses the artwork’s own colours, gradients and images (rasterised layer texture).',
  none: 'No paint — glass shows pure refraction, solids render neutral.',
  'system-light': 'Apple’s light background gradient (#ffffff → #e4e5ea).',
  'system-dark': 'Apple’s dark background gradient (#3a3a3f → #111114).',
}

function Chip({ fill }: { fill: Fill }) {
  return (
    <span className="relative inline-block h-3.5 w-3.5 overflow-hidden rounded-[4px] border border-white/20 checkerboard-sm">
      <span className="absolute inset-0" style={{ background: fillToCss(fill) }} />
    </span>
  )
}

export function FillEditor({
  fill,
  onChange,
  types = ['auto', 'none', 'solid', 'linear', 'radial', 'system-light', 'system-dark'],
  label = 'Fill',
  disabled,
  fallbackColor,
}: {
  fill: Fill
  onChange: (fill: Fill, phase: ChangePhase) => void
  types?: FillType[]
  label?: string
  disabled?: boolean
  fallbackColor?: string
}) {
  const options: SelectOption<FillType>[] = types.map((t) => ({
    value: t,
    label: FILL_LABEL[t],
    icon: <Chip fill={convertFill(t, fill, fallbackColor)} />,
  }))

  return (
    <div className={cn('space-y-[7px]', disabled && 'pointer-events-none opacity-45')}>
      <Row label={label}>
        <Select value={fill.type} options={options} onChange={(t) => t !== fill.type && onChange(convertFill(t, fill, fallbackColor), 'commit')} className="flex-1" />
      </Row>
      {FILL_HELP[fill.type] && <p className="pl-[92px] text-3xs leading-snug text-fg-4">{FILL_HELP[fill.type]}</p>}
      {fill.type === 'solid' && (
        <Row label="Colour">
          <ColorField
            value={fill.color}
            alpha={fill.opacity}
            onChange={(c, p) => onChange({ ...fill, color: c }, p)}
            onAlphaChange={(a, p) => onChange({ ...fill, opacity: a }, p)}
            className="flex-1"
          />
        </Row>
      )}
      {fill.type === 'linear' && <LinearControls fill={fill} onChange={onChange} />}
      {fill.type === 'radial' && <RadialControls fill={fill} onChange={onChange} />}
    </div>
  )
}

function LinearControls({ fill, onChange }: { fill: FillLinear; onChange: (f: Fill, p: ChangePhase) => void }) {
  const angle = Math.round(linearAngle(fill.start, fill.end))
  const center: [number, number] = [(fill.start[0] + fill.end[0]) / 2, (fill.start[1] + fill.end[1]) / 2]
  const half = Math.max(0.05, Math.hypot(fill.end[0] - fill.start[0], fill.end[1] - fill.start[1]) / 2)
  return (
    <div className="pl-[92px]">
      <GradientEditor
        stops={fill.stops}
        onChange={(stops, p) => onChange({ ...fill, stops }, p)}
        angle={angle}
        onAngleChange={(deg, p) => onChange({ ...fill, ...linearPointsFromAngle(deg, center, half) }, p)}
      />
    </div>
  )
}

function RadialControls({ fill, onChange }: { fill: FillRadial; onChange: (f: Fill, p: ChangePhase) => void }) {
  const c = fill.center ?? [0, 0]
  return (
    <>
      <div className="pl-[92px]">
        <GradientEditor stops={fill.stops} onChange={(stops, p) => onChange({ ...fill, stops }, p)} />
      </div>
      <Row label="Centre">
        <NumberField prefix="X" value={c[0]} min={-1.5} max={1.5} step={0.01} onChange={(v, p) => onChange({ ...fill, center: [v, c[1]] }, p)} className="flex-1" />
        <NumberField prefix="Y" value={c[1]} min={-1.5} max={1.5} step={0.01} onChange={(v, p) => onChange({ ...fill, center: [c[0], v] }, p)} className="flex-1" />
      </Row>
      <Row label="Radius">
        <NumberField value={fill.radius} min={0.05} max={3} step={0.01} onChange={(v, p) => onChange({ ...fill, radius: v }, p)} className="flex-1" />
      </Row>
    </>
  )
}
