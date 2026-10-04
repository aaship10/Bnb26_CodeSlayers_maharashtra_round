import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests against the PRODUCTION build (vite preview, same proxy layout
 * as nginx) and the mock server. Run: npm run e2e  (builds first).
 * Tests share one mock, so they run one at a time and each sets its own scenario.
 */
const PREVIEW = 'http://localhost:4173';
/** Set to test a deployed stack instead, e.g. E2E_BASE_URL=http://localhost:8080 (nginx). */
const BASE = process.env.E2E_BASE_URL ?? PREVIEW;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: BASE,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    reducedMotion: 'reduce',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 7'] }, testMatch: /attendee|a11y/ },
  ],
  webServer: [
    {
      command: 'npx tsx mock-server/src/index.ts',
      url: 'http://127.0.0.1:8787/__mock/state',
      reuseExistingServer: true,
      env: { MOCK_SIM_SPEED: '10' },
      timeout: 30_000,
    },
    ...(process.env.E2E_BASE_URL
      ? []
      : [
          {
            command: 'npx vite preview --port 4173 --strictPort',
            url: PREVIEW,
            reuseExistingServer: true,
            timeout: 30_000,
          },
        ]),
  ],
});
