import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Backend (FastAPI) runs on 8420; in dev, Vite proxies API, websocket and file routes to it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8420', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8420', ws: true },
      '/files': { target: 'http://127.0.0.1:8420', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 4000 },
})
