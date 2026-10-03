import { useRef } from 'react'
import { Activity, Cpu, Gauge, Keyboard, ListOrdered, RotateCcw, Shapes, Thermometer, Timer, Wifi, WifiOff } from 'lucide-react'
import { errorMessage, systemApi } from '../../api'
import { cn, formatMB, formatSeconds, shortGpuName } from '../../lib/format'
import { useAppStore } from '../../store/app'
import { useEditor } from '../../store/editor'
import { useRender } from '../../store/render'
import { toast } from '../../store/toasts'
import { useUi } from '../../store/ui'
import { Menu, Spinner, StatusDot, useAnchor, type DotTone } from '../../components/ui'

const WORKER_LABEL = { stopped: 'Worker stopped', starting: 'Worker starting…', ready: 'Worker ready', busy: 'Rendering', error: 'Worker error' } as const

export function StatusBar() {
  const system = useAppStore((s) => s.system)
  const systemError = useAppStore((s) => s.systemError)
  const ws = useAppStore((s) => s.ws)
  const lastDone = useRender((s) => (s.lastDone ? s.entries[s.lastDone] : null))
  const geometryLoading = useEditor((s) => s.geometryLoading)
  const geometryError = useEditor((s) => s.geometryError)
  const layerCount = useEditor((s) => s.project?.layers.length ?? 0)
  const elementCount = useEditor((s) => s.project?.elements.length ?? 0)
  const busy = useEditor((s) => s.busy)
  const menu = useAnchor<HTMLButtonElement>()
  const workerRef = useRef<HTMLButtonElement>(null)

  const w = system?.worker.state
  const tone: DotTone = !system ? (systemError ? 'bad' : 'idle') : !system.blender.found ? 'bad' : w === 'ready' ? 'ok' : w === 'busy' ? 'busy' : w === 'starting' ? 'warn' : w === 'error' ? 'bad' : 'idle'
  const gpu = system?.gpu
  const used = gpu?.memoryUsedMB
  const total = gpu?.memoryTotalMB
  const vramPct = used != null && total ? used / total : null
  const queued = (system?.queue.queued ?? 0) + (system?.queue.running ?? 0)

  return (
    <footer className="relative z-20 flex h-7 shrink-0 items-center gap-3 overflow-hidden whitespace-nowrap border-t border-line bg-surface-1 px-2.5 text-3xs text-fg-3">
      <button
        ref={workerRef}
        type="button"
        onClick={() => workerRef.current && menu.toggle(workerRef.current)}
        className="flex h-5 items-center gap-1.5 rounded px-1.5 transition-colors hover:bg-white/[0.06] hover:text-fg-2"
        data-tip={system?.worker.message || (system?.blender.path ? `Blender: ${system.blender.path}` : 'Blender worker')}
      >
        <StatusDot tone={tone} pulse={w === 'starting' || w === 'busy'} />
        <span className="font-medium text-fg-2">
          {!system ? (systemError ? 'Server offline' : 'Connecting…') : !system.blender.found ? 'Blender 5.0 not found' : WORKER_LABEL[w ?? 'stopped']}
        </span>
        {system?.blender.version && <span className="text-fg-4">· Blender {system.blender.version.split(' ')[0]}</span>}
      </button>

      {gpu?.name && (
        <>
          <Sep />
          <span className="flex items-center gap-1.5" data-tip={gpu.name}>
            <Cpu className="h-3 w-3" />
            <span className="font-semibold tracking-wide text-accent-2">{gpu.device ?? 'OPTIX'}</span>
            <span className="text-fg-2">{shortGpuName(gpu.name)}</span>
          </span>
        </>
      )}
      {vramPct != null && (
        <span className="flex items-center gap-1.5" data-tip={`VRAM ${formatMB(used)} of ${formatMB(total)} (shared with the desktop)`}>
          <span className="text-fg-4">VRAM</span>
          <span className="relative h-[5px] w-16 overflow-hidden rounded-full bg-white/[0.08]">
            <span
              className={cn('absolute inset-y-0 left-0 rounded-full transition-[width] duration-500', vramPct > 0.9 ? 'bg-bad' : vramPct > 0.75 ? 'bg-warn' : 'accent-gradient')}
              style={{ width: `${Math.max(3, vramPct * 100)}%` }}
            />
          </span>
          <span className="tabular text-fg-2">
            {((used ?? 0) / 1024).toFixed(1)}/{((total ?? 0) / 1024).toFixed(1)} GB
          </span>
        </span>
      )}
      {gpu?.utilization != null && (
        <span className="hidden items-center gap-1 tabular lg:flex" data-tip="GPU utilisation">
          <Gauge className="h-3 w-3" />
          <span className="w-7 text-fg-2">{Math.round(gpu.utilization)}%</span>
        </span>
      )}
      {gpu?.temperature != null && (
        <span className="hidden items-center gap-0.5 tabular xl:flex" data-tip="GPU temperature">
          <Thermometer className="h-3 w-3" />
          <span className={cn(gpu.temperature > 80 ? 'text-warn' : 'text-fg-2')}>{Math.round(gpu.temperature)}°C</span>
        </span>
      )}
      <Sep />
      <span className="flex items-center gap-1" data-tip="Jobs in the GPU queue">
        <ListOrdered className="h-3 w-3" />
        {queued > 0 ? (
          <span className="text-fg-2">
            {system?.queue.running ? `${system.queue.running} running` : ''}
            {system?.queue.running && system?.queue.queued ? ' · ' : ''}
            {system?.queue.queued ? `${system.queue.queued} queued` : ''}
          </span>
        ) : (
          <span>Queue idle</span>
        )}
      </span>

      <div className="flex-1" />

      {busy && (
        <span className="flex items-center gap-1.5 text-fg-2">
          <Spinner className="h-3 w-3" /> {busy}…
        </span>
      )}
      {geometryLoading ? (
        <span className="flex items-center gap-1.5">
          <Spinner className="h-3 w-3" /> Building geometry…
        </span>
      ) : geometryError ? (
        <span className="flex items-center gap-1 text-bad" data-tip={geometryError}>
          <Shapes className="h-3 w-3" /> Geometry error
        </span>
      ) : (
        <span className="hidden items-center gap-1 xl:flex" data-tip="Layers · SVG elements">
          <Shapes className="h-3 w-3" /> {layerCount} layers · {elementCount} elements
        </span>
      )}
      {lastDone && (
        <>
          <Sep />
          <span className="flex items-center gap-1" data-tip={`Last render · ${lastDone.appearance} · ${lastDone.width ?? ''}px`}>
            <Timer className="h-3 w-3" />
            <span className="tabular text-fg-2">{formatSeconds(lastDone.seconds)}</span>
            <span className="text-fg-4">· {lastDone.engine ?? (lastDone.quality === 'draft' ? 'EEVEE' : 'Cycles')} {lastDone.quality}</span>
          </span>
        </>
      )}
      <Sep />
      <span data-tip={ws === 'open' ? 'Live updates connected' : ws === 'connecting' ? 'Connecting live updates…' : 'Live updates disconnected — retrying'} className="flex items-center">
        {ws === 'open' ? <Wifi className="h-3 w-3 text-ok/80" /> : ws === 'connecting' ? <Activity className="h-3 w-3 animate-pulse text-warn" /> : <WifiOff className="h-3 w-3 text-bad" />}
      </span>
      <button
        type="button"
        onClick={() => useUi.getState().openDialog('shortcuts')}
        className="flex h-5 items-center gap-1 rounded px-1 transition-colors hover:bg-white/[0.06] hover:text-fg-2"
        data-tip="Keyboard shortcuts"
        aria-label="Keyboard shortcuts"
        data-tip-kbd="?"
      >
        <Keyboard className="h-3 w-3" />
      </button>

      <Menu
        open={menu.open}
        onClose={menu.close}
        anchor={menu.anchor}
        placement="top-start"
        width={260}
        items={[
          { type: 'label', label: system?.blender.path ?? 'Blender worker' },
          {
            label: 'Restart worker',
            icon: <RotateCcw />,
            description: 'Kills the persistent Blender process and starts a fresh one (frees VRAM).',
            onSelect: async () => {
              try {
                await systemApi.restartWorker()
                toast.info('Restarting Blender worker…')
                void useAppStore.getState().refreshSystem()
              } catch (e) {
                toast.error('Restart failed', { description: errorMessage(e) })
              }
            },
          },
        ]}
      />
    </footer>
  )
}

function Sep() {
  return <span className="h-3 w-px bg-line-2" />
}
