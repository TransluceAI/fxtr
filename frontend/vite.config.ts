import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// The dev server forwards the viewer's API to a running `fxtr view` (FXTR_DEV_BACKEND overrides
// where it listens), so the dev build uses the same origin-relative URLs as the built viewer.
const backend = process.env.FXTR_DEV_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  css: {
    // Modules are written kebab-case and read camelCase: `.step-card` is `styles.stepCard`.
    modules: { localsConvention: 'camelCaseOnly' },
  },
  server: {
    proxy: { '/api': { target: backend, ws: true } },
  },
  test: {
    setupFiles: ['test/setup.ts'],
  },
})
