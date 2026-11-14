import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// The driving service. The dev server (and `vite preview`) proxy its API and
// socket so the app talks to it on its own origin, as the service's origin
// check requires.
const SERVICE_URL = process.env.DRIVE_SERVICE_URL ?? 'http://127.0.0.1:8000'

// changeOrigin stays false: the service accepts a browser request only when its
// Origin matches the Host it was sent to, so the Host must pass through unchanged.
// (Vite's string shorthand would rewrite it to the service's own address.)
const proxy = {
  '/api': { target: SERVICE_URL, changeOrigin: false },
  '/ws': { target: SERVICE_URL, ws: true, changeOrigin: false },
}

export default defineConfig({
  plugins: [react()],
  // Loopback only, like the service: with the proxy, listening on the LAN would
  // hand the phone's camera and location to anyone on the same Wi-Fi.
  // The iPad reaches it through `tailscale serve` instead.
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy,
    // Vite's default CORS lets any localhost page read through the proxy (the
    // phone's location) and set the destination; the app needs no CORS at all.
    cors: false,
    // Reached on the iPad through `tailscale serve`, under the Mac's *.ts.net name.
    allowedHosts: ['.ts.net'],
  },
  preview: { host: '127.0.0.1', port: 5174, strictPort: true, proxy, cors: false, allowedHosts: ['.ts.net'] },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/services/live/**/*.ts', 'src/services/sensors/**/*.ts', 'src/services/goals.ts', 'src/components/drive/ChairLinkChip.tsx'],
      exclude: ['**/*.test.ts', '**/testing.ts'],
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
    },
  },
})
