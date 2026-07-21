import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    allowedHosts: ['127.0.0.1'],
    strictPort: true,
    port: 48272,
    proxy: {
      '/api': 'http://127.0.0.1:48271',
    },
  },
  preview: {
    host: '127.0.0.1',
    allowedHosts: ['127.0.0.1'],
    strictPort: true,
  },
})
