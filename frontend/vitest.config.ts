import { defineConfig } from 'vitest/config';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    globals: true,
    // Default is node; component/storage tests opt in with `// @vitest-environment jsdom`.
    environment: 'node',
    // keep mock-server proof-of-work cheap in tests (the dev server defaults to 18 bits)
    env: { MOCK_POW_BITS: '12' },
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'mock-server/**/*.test.ts'],
  },
});
