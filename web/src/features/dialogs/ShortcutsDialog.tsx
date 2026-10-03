import { Keyboard } from 'lucide-react'
import { useUi } from '../../store/ui'
import { Dialog, Kbd } from '../../components/ui'
import { SHORTCUTS, type ShortcutDef } from '../editor/shortcuts'

const GROUPS: ShortcutDef['group'][] = ['General', 'View', 'Layers', 'Render']

export function ShortcutsDialog() {
  const open = useUi((s) => s.dialog === 'shortcuts')
  return (
    <Dialog open={open} onClose={() => useUi.getState().openDialog(null)} width={720} icon={<Keyboard />} title="Keyboard shortcuts" description="Work at the speed of thought. Shortcuts are ignored while typing in a field.">
      <div className="grid grid-cols-2 gap-x-8 gap-y-5">
        {GROUPS.map((g) => (
          <div key={g}>
            <div className="mb-2 text-3xs font-semibold uppercase tracking-[0.09em] text-fg-4">{g}</div>
            <div className="space-y-1">
              {SHORTCUTS.filter((s) => s.group === g).map((s) => (
                <div key={s.keys} className="flex items-center gap-3 rounded-md px-1 py-[3px] text-2xs hover:bg-white/[0.03]">
                  <span className="min-w-0 flex-1 text-fg-2">{s.label}</span>
                  <span className="flex shrink-0 items-center gap-0.5">
                    {s.keys.includes('+') ? s.keys.split('+').map((k) => <Kbd key={k}>{k}</Kbd>) : <Kbd>{s.keys}</Kbd>}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Dialog>
  )
}
