import { lazy, Suspense, useEffect } from 'react'
import { useRoute } from './lib/route'
import { isTypingTarget } from './lib/hooks'
import { startAppServices } from './store/app'
import { useUi } from './store/ui'
import HomePage from './features/home/HomePage'
import { EditorSkeleton } from './features/editor/EditorSkeleton'
import { ImportDialog } from './features/dialogs/ImportDialog'
import { ShortcutsDialog } from './features/dialogs/ShortcutsDialog'
import { ConfirmHost, Spinner, Toaster, TooltipLayer } from './components/ui'

// The editor pulls in three.js — keep it out of the home screen bundle.
const EditorPage = lazy(() => import('./features/editor/EditorPage'))
const PackPage = lazy(() => import('./features/pack/PackPage'))

export default function App() {
  const route = useRoute()

  useEffect(() => {
    startAppServices()
  }, [])

  // Global: "?" opens the shortcut sheet anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return
      if (e.key === '?' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault()
        const ui = useUi.getState()
        ui.openDialog(ui.dialog === 'shortcuts' ? null : 'shortcuts')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    document.title =
      route.name === 'home' ? 'Blender Icon Studio' : route.name === 'pack' ? 'Icon Pack · Blender Icon Studio' : 'Editor · Blender Icon Studio'
  }, [route])

  return (
    <>
      {route.name === 'home' ? (
        <HomePage />
      ) : route.name === 'pack' ? (
        <Suspense fallback={<PageFallback />}>
          <PackPage />
        </Suspense>
      ) : (
        <Suspense fallback={<EditorSkeleton />}>
          <EditorPage key={route.projectId} projectId={route.projectId} />
        </Suspense>
      )}
      <ImportDialog />
      <ShortcutsDialog />
      <ConfirmHost />
      <Toaster />
      <TooltipLayer />
    </>
  )
}

function PageFallback() {
  return (
    <div className="flex h-full items-center justify-center bg-app">
      <Spinner />
    </div>
  )
}
