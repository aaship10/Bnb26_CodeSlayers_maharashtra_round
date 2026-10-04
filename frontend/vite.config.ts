import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath, URL } from 'node:url';

// In dev we mimic the nginx layout from the shared conventions:
//   /api/*  -> backend (prefix stripped)     [mock server for now]
//   /sim/*  -> simulator service
//   /__mock -> mock-server control endpoints (dev only)
const MOCK_TARGET = process.env.MOCK_URL ?? 'http://127.0.0.1:8787';
const API_TARGET = process.env.API_URL ?? 'http://127.0.0.1:8000';
const SIM_TARGET = process.env.SIM_URL ?? 'http://127.0.0.1:8100';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/sim': { target: SIM_TARGET, changeOrigin: true },
      '/__mock': { target: MOCK_TARGET, changeOrigin: true },
    },
  },
  build: {
    sourcemap: true,
    target: 'es2020',
    // Never inline fonts as data: URIs; the production CSP (font-src 'self') blocks them.
    assetsInlineLimit: (file) => (/\.(woff2?|ttf|otf)$/.test(file) ? false : undefined),
  },
});
