/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';

const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string };

export default defineConfig({
  base: '/static/dashboard/',
  plugins: [react()],
  define: { __APP_VERSION__: JSON.stringify(version) },
  test: {
    exclude: ['tests/browser/**', 'node_modules/**', 'dist/**'],
  },
  build: {
    outDir: 'static/dashboard',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        dashboard: fileURLToPath(new URL('./index.html', import.meta.url)),
        admin: fileURLToPath(new URL('./admin.html', import.meta.url)),
      },
      output: {
        // React and its DOM renderer change rarely: one named, long-cached
        // vendor chunk instead of an anonymous shared chunk.
        manualChunks(id) {
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'vendor-react';
          return undefined;
        },
      },
    },
    // hls.js is intentionally split into its own on-demand chunk, so the
    // default 500 kB warning would only flag that expected lazy bundle.
    chunkSizeWarningLimit: 600,
  },
});
