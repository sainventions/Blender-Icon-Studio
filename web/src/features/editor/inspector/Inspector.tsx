import { FileSliders, Layers, Sparkles } from 'lucide-react'
import { useEditor } from '../../../store/editor'
import { useUi, type InspectorTab } from '../../../store/ui'
import { Segmented } from '../../../components/ui'
import { LayerInspector } from './LayerInspector'
import { DocumentInspector } from './DocumentInspector'
import { RenderInspector } from './RenderInspector'

export function Inspector() {
  const tab = useUi((s) => s.inspectorTab)
  const set = useUi((s) => s.set)
  const selCount = useEditor((s) => s.selection.layerIds.length)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-9 shrink-0 items-center border-b border-line px-2">
        <Segmented<InspectorTab>
          size="sm"
          fill
          value={tab}
          onChange={(v) => set({ inspectorTab: v })}
          options={[
            { value: 'layer', icon: <Layers />, label: selCount > 1 ? `Layers (${selCount})` : 'Layer' },
            { value: 'document', icon: <FileSliders />, label: 'Document' },
            { value: 'render', icon: <Sparkles />, label: 'Render' },
          ]}
          className="w-full"
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden" key={tab}>
        {tab === 'layer' && <LayerInspector />}
        {tab === 'document' && <DocumentInspector />}
        {tab === 'render' && <RenderInspector />}
      </div>
    </div>
  )
}
