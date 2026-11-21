// Helpers for end-to-end tests (outside src: not compiled or shipped
// with the package).
import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const VENV_PY = join(ROOT, '.venv', 'bin', 'python');
export const PY = process.env.VEXINTER_PYTHON || (existsSync(VENV_PY) ? VENV_PY : 'python');
const LAUNCHER = join(ROOT, 'packages', 'sdk-ts', 'scripts', 'serve_test.py');
export const ADMIN = 'tok-e2e-sdk-ts-1234';

export const probeError = (() => {
  const r = spawnSync(
    PY, ['-c', 'import sys; sys.path.insert(0, sys.argv[1]); import services.app', ROOT],
    // services.app runs make_app() at import, which requires an admin
    // token outside dev mode — provide a dummy one for the probe.
    { cwd: ROOT, timeout: 30_000,
      env: { ...process.env, VEXINTER_ADMIN_TOKEN: 'e2e-probe-token-0123456789abcdef' } });
  if (r.status === 0) return '';
  return `status=${r.status} error=${r.error ?? ''} ` +
    (r.stderr || r.stdout || '').toString().slice(-1500);
})();

export const hasService = probeError === '';

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address() as { port: number };
      s.close(() => resolve(port));
    });
  });
}

export interface Service { base: string; stop: () => void }

export async function startService(extra: string[] = []): Promise<Service> {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  let log = '';
  const proc = spawn(PY, [LAUNCHER, String(port), ...extra],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  proc.stdout?.on('data', (d) => { log += d; });
  proc.stderr?.on('data', (d) => { log += d; });
  const deadline = Date.now() + 25_000;
  for (;;) {
    try {
      const r = await fetch(`${base}/openapi.json`);
      if (r.ok) break;
    } catch { /* still starting */ }
    if (Date.now() > deadline) {
      proc.kill();
      throw new Error(`service did not start:\n${log}`);
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return { base, stop: () => { proc.kill(); } };
}
