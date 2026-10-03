import { useEffect, useRef, useState } from 'react'
import { toast } from '../../store/toasts'

export function isSvgFile(f: File): boolean {
  return f.type === 'image/svg+xml' || /\.svg$/i.test(f.name)
}

/** Window-wide file drag & drop. Returns true while a file is being dragged over the window. */
export function useFileDrop(onFile: (file: File) => void, enabled = true): boolean {
  const [dragging, setDragging] = useState(false)
  const depth = useRef(0)
  const cb = useRef(onFile)
  cb.current = onFile

  useEffect(() => {
    if (!enabled) return
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes('Files')
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      depth.current++
      setDragging(true)
    }
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    }
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return
      depth.current = Math.max(0, depth.current - 1)
      if (depth.current === 0) setDragging(false)
    }
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      depth.current = 0
      setDragging(false)
      const files = Array.from(e.dataTransfer?.files ?? [])
      const svg = files.find(isSvgFile)
      if (svg) cb.current(svg)
      else if (files.length) toast.warning('Only SVG files can be imported', { description: files[0].name })
    }
    window.addEventListener('dragenter', enter)
    window.addEventListener('dragover', over)
    window.addEventListener('dragleave', leave)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragenter', enter)
      window.removeEventListener('dragover', over)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('drop', drop)
    }
  }, [enabled])

  return dragging
}
