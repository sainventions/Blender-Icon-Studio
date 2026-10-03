import { Skeleton } from '../../components/ui'
import { Logo } from '../../components/icons'

/** Placeholder layout while the editor bundle / project loads. */
export function EditorSkeleton({ label = 'Loading editor…' }: { label?: string }) {
  return (
    <div className="flex h-full flex-col bg-app">
      <div className="flex h-11 items-center gap-3 border-b border-line bg-surface-1 px-3">
        <Logo size={20} />
        <Skeleton className="h-3 w-40" />
        <div className="flex-1" />
        <Skeleton className="h-6 w-72 rounded-lg" />
        <div className="flex-1" />
        <Skeleton className="h-6 w-24" />
        <Skeleton className="h-6 w-20" />
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="w-[256px] space-y-2 border-r border-line bg-surface-1 p-3">
          <Skeleton className="h-4 w-24" />
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="flex items-center gap-2">
              <Skeleton className="h-7 w-7" />
              <Skeleton className="h-3 flex-1" />
            </div>
          ))}
        </div>
        <div className="relative flex flex-1 items-center justify-center stage-backdrop">
          <div className="flex flex-col items-center gap-4">
            <Skeleton className="h-[260px] w-[260px] rounded-[22%]" />
            <span className="text-2xs text-fg-4">{label}</span>
          </div>
        </div>
        <div className="w-[316px] space-y-3 border-l border-line bg-surface-1 p-3">
          <Skeleton className="h-7 w-full rounded-lg" />
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="flex items-center gap-2">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-3 flex-1" />
            </div>
          ))}
        </div>
      </div>
      <div className="h-7 border-t border-line bg-surface-1" />
    </div>
  )
}
