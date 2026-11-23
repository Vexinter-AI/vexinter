import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// En desarrollo la interfaz se sirve en /; compilada, la sirve el servicio en /app/.
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/app/' : '/',
  plugins: [react()],
  // Inline empty PostCSS: without this, Vite looks for postcss.config.* in
  // parent folders and picks up the root Next.js site's (Tailwind),
  // which does not belong to this UI.
  css: { postcss: { plugins: [] } },
  server: {
    port: 5173,
    proxy: { '/v1': { target: 'http://localhost:8000', changeOrigin: true } },
  },
}));
