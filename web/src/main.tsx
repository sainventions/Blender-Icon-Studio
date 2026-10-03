import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'

if (import.meta.env.DEV) {
  // Dev-only handle for scripted UI checks (headless browser, screenshots): the live store instances. Dynamic imports
  // inside the DEV branch, so production chunks are unchanged.
  void Promise.all([import('./store/app'), import('./store/editor'), import('./store/render'), import('./store/ui')]).then(
    ([app, editor, render, ui]) => {
      ;(window as unknown as { __bis?: unknown }).__bis = {
        useAppStore: app.useAppStore,
        useEditor: editor.useEditor,
        useRender: render.useRender,
        useUi: ui.useUi,
      }
    },
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
