/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Proxy targets mirror every path prefix the old dashboard talked to directly
// (see notes/api-contract-actual.md §1) so `npm run dev` behaves like the
// production deployment, where kenny-server serves both the API and this
// build's static output from the same origin.
const PROXIED_PREFIXES = [
  '/api',
  '/auth',
  '/chat',
  '/tickets',
  '/users',
  '/download',
  '/login',
  '/logout',
]

export default defineConfig({
  plugins: [react()],
  build: {
    // kenny-server serves the built SPA from here (kenny-server/CLAUDE.md).
    outDir: '../kenny-server/kenny_server/webui/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: Object.fromEntries(
      PROXIED_PREFIXES.map((prefix) => [
        prefix,
        { target: 'http://localhost:8787', changeOrigin: true },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
