import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const configuredBasePath = process.env.VITE_PUBLIC_BASE_PATH?.trim() || '/'
const publicBasePath = configuredBasePath === '/'
  ? '/'
  : `/${configuredBasePath.replace(/^\/+|\/+$/g, '')}/`

export default defineConfig({
  base: publicBasePath,
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 4173,
  },
})
