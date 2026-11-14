import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';

// Separate port from a dev service so the test never talks to a real session.
const PORT = 8766;
const webDist = fileURLToPath(new URL('./dist', import.meta.url));
const serviceDir = fileURLToPath(new URL('../service', import.meta.url));

export default defineConfig({
  testDir: 'tests/e2e',
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium-fake-sensors',
      use: {
        ...devices['Desktop Chrome'],
        permissions: ['camera', 'geolocation'],
        geolocation: { latitude: 38.9869, longitude: -76.9426, accuracy: 5 },
        launchOptions: {
          args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
        },
      },
    },
  ],
  webServer: {
    command: 'npm run build && cd "$SERVICE_DIR" && uv run python -m drive_service',
    url: `http://127.0.0.1:${PORT}/api/status`,
    timeout: 120_000,
    reuseExistingServer: false,
    env: {
      SERVICE_DIR: serviceDir,
      DRIVE_PORT: String(PORT),
      DRIVE_WEB_DIST: webDist,
      DRIVE_RECORD_DIR: join(tmpdir(), 'fsd-wheelchair-e2e-recordings'),
    },
  },
});
