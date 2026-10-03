// Blender render image with a blur-crossfade whenever the URL changes (decoded before swapping).
import { useEffect, useState } from 'react'
import { ImageOff } from 'lucide-react'
import { cn } from '../../../lib/format'

export function RenderImage({ url, className, onError, alt = 'Blender render' }: { url: string | null | undefined; className?: string; onError?: () => void; alt?: string }) {
  const [layers, setLayers] = useState<{ url: string; key: number }[]>([])
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    if (!url) {
      setLayers([])
      return
    }
    if (layers.length && layers[layers.length - 1].url === url) return
    let cancelled = false
    const img = new Image()
    img.decoding = 'async'
    img.src = url
    const show = () => {
      if (cancelled) return
      setFailed(null)
      setLayers((ls) => [...ls.slice(-1), { url, key: performance.now() }])
    }
    img
      .decode()
      .then(show)
      .catch(() => {
        if (cancelled) return
        if (img.complete && img.naturalWidth > 0) show()
        else {
          setFailed(url)
          onError?.()
        }
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url])

  return (
    <div className={cn('relative h-full w-full', className)}>
      {layers.map((l, i) => {
        const top = i === layers.length - 1
        return (
          <img
            key={l.key}
            src={l.url}
            alt={alt}
            draggable={false}
            onAnimationEnd={() => top && setLayers((ls) => ls.slice(-1))}
            className="absolute inset-0 h-full w-full select-none object-contain"
            style={top && layers.length > 1 ? { animation: 'crossfade-in 300ms cubic-bezier(0.16,1,0.3,1) both' } : undefined}
          />
        )
      })}
      {failed && !layers.length && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-fg-4">
          <ImageOff className="h-6 w-6" />
          <span className="text-2xs">Render image unavailable</span>
        </div>
      )}
    </div>
  )
}
