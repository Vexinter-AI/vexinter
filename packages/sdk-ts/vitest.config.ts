import { defineConfig } from 'vitest/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    cache: {
      dir: '/tmp/opencode/vitest-cache'
    },
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      exclude: ['src/**/*.test.ts', 'src/**/*.d.ts']
    },
    testTimeout: 10000
  },
  resolve: {
    alias: {
      '@vexinter-ai/sdk': path.resolve(__dirname, './src')
    }
  }
});