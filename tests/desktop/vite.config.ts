import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// Builds the player test harness (tests/desktop/harness.html) for
// scripts/desktop-player-e2e.mjs. HARNESS_OUT is a temporary folder.
export default defineConfig({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  base: '/harness/',
  plugins: [react()],
  define: { __APP_VERSION__: JSON.stringify('harness') },
  build: {
    outDir: process.env.HARNESS_OUT,
    emptyOutDir: true,
    rollupOptions: { input: fileURLToPath(new URL('./harness.html', import.meta.url)) },
  },
});
