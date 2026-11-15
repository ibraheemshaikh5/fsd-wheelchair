import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// The driving service. In dev, Vite proxies API and socket traffic to it so the
// pages behave as they do when the service serves the built files itself.
const SERVICE_URL = process.env.DRIVE_SERVICE_URL ?? 'http://127.0.0.1:8000';

const page = (path: string): string => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rolldownOptions: {
      input: {
        home: page('./index.html'),
        capture: page('./capture/index.html'),
        viewer: page('./viewer/index.html'),
      },
    },
  },
  server: {
    // Vite's default CORS lets any localhost page read the dev server's responses,
    // and /api/status carries live GPS. The service never needs cross-origin access.
    cors: false,
    proxy: {
      // Object form: the string shorthand sets changeOrigin, which rewrites Host to the
      // service's address so the service's same-origin check refuses every POST.
      '/api': { target: SERVICE_URL, changeOrigin: false },
      '/ws': { target: SERVICE_URL, ws: true, changeOrigin: false },
    },
  },
  preview: {
    cors: false,
  },
  test: {
    include: ['tests/unit/**/*.test.ts'],
    environment: 'node',
  },
});
