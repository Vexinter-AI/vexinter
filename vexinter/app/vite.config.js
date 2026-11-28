import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In dev the UI is served at /; compiled, the service serves it at /vexinter/.
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/vexinter/' : '/',
  plugins: [react()],
  // Inline empty PostCSS: without this, Vite looks for postcss.config.* in
  // parent folders and picks up the root Next.js site's (Tailwind),
  // which does not belong to this UI.
  css: { postcss: { plugins: [] } },
  server: {
    port: 5173,
    proxy: { '/v1': { target: process.env.VEXINTER_API || 'http://localhost:8000', changeOrigin: true } },
  },
}));
