// The SDK against the REAL Python service (not a mocked fetch).
// Skipped only when there is no Python environment with Vexinter installed.
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { createAgentKeyMaterial } from '../crypto';
import { buildAuthHeaders } from '../identity';
import { AsyncHttpClient, retryConfig } from './client';
import { AsyncRemoteRuntime } from './runtime';

retryConfig.baseMs = 1;
vi.setConfig({ testTimeout: 30_000, hookTimeout: 45_000 });

const ROOT = fileURLToPath(new URL('../../../../', import.meta.url));
const VENV_PY = join(ROOT, '.venv', 'bin', 'python');
const PY = process.env.VEXINTER_PYTHON || (existsSync(VENV_PY) ? VENV_PY : 'python');
const LAUNCHER = join(ROOT, 'packages', 'sdk-ts', 'scripts', 'serve_test.py');
const ADMIN = 'tok-e2e-sdk-ts-1234';

const probeError = (() => {
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
const hasService = probeError === '';

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

const COND_CUSTOMER = {
  op: 'exists', system: 'crm',
  query: { type: 'customer', where: { email: { $ref: 'input.email' } } },
};
const COND_ONE_INVOICE = {
  op: 'all', args: [
    { ...COND_CUSTOMER, as: 'customer' },
    { op: 'count', system: 'billing',
      query: { type: 'invoice',
               where: { customer_id: { $ref: 'customer.id' }, status: 'issued' } },
      equals: 1 },
  ],
};

// In CI the Python environment is mandatory: without this, the test would be
// silently skipped and a change breaking signatures would pass unnoticed.
if (!hasService && process.env.VEXINTER_E2E_REQUIRED) {
  describe('TypeScript SDK against the real Python service', () => {
    it('the Python environment with Vexinter is required', () => {
      throw new Error('VEXINTER_E2E_REQUIRED is set but there is no Python environment with Vexinter\nprobe: ' + probeError);
    });
  });
}

describe.skipIf(!hasService)('TypeScript SDK against the real Python service', () => {
  let proc: ChildProcess | undefined;
  let base = '';
  let log = '';

  beforeAll(async () => {
    const port = await freePort();
    base = `http://127.0.0.1:${port}`;
    proc = spawn(PY, [LAUNCHER, String(port)],
      { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
    proc.stdout?.on('data', (d) => { log += d; });
    proc.stderr?.on('data', (d) => { log += d; });
    const deadline = Date.now() + 25_000;
    for (;;) {
      try {
        const r = await fetch(`${base}/openapi.json`);
        if (r.ok) break;
      } catch { /* still starting */ }
      if (Date.now() > deadline) throw new Error(`service did not start:\n${log}`);
      await new Promise((r) => setTimeout(r, 200));
    }
  });
  afterAll(() => { proc?.kill(); });
  afterEach(() => vi.unstubAllGlobals());

  const enroll = async (agent: string) => {
    const adm = new AsyncRemoteRuntime({ endpoint: base, adminToken: ADMIN });
    await adm.enrollAgent(agent,
      { allowedTools: ['crm.create_customer', 'billing.create_invoice'] });
    const inst = await adm.spawnInstance(agent);
    return adm.issueCredential(inst.instance_id, createAgentKeyMaterial());
  };
  const task = async (agent: string, creds: Awaited<ReturnType<typeof enroll>>,
                      condition: unknown, email: string) => {
    const rt = new AsyncRemoteRuntime({ endpoint: base, credentials: creds });
    return rt.task({ agent, objective: 'Test from TypeScript',
                     condition, input: { email }, credentials: creds }).enter();
  };

  it('signed enrollment, invocation, verification, audit and exit', async () => {
    const creds = await enroll('bot-ts-1');
    const t = await task('bot-ts-1', creds, COND_CUSTOMER, 'ana@example.com');
    const r = await t.tools.crm.create_customer(
      { name: 'Ana', email: 'ana@example.com' });
    expect(r.ok).toBe(true);
    expect((await t.verify()).status).toBe('met');
    expect(((await t.audit()) as unknown as { ok: boolean }).ok).toBe(true);
    expect(String(await t.getState())).toBe('satisfied');
    await t.exit();
  });

  it('without credentials the data plane responds 401', async () => {
    const http = new AsyncHttpClient({ endpoint: base });
    await expect(http.request('POST', '/v1/tasks', {
      agent: 'x', objective: 'o', condition: COND_CUSTOMER, input: {} },
    )).rejects.toMatchObject({ statusCode: 401 });
  });

  it('a lost response is retried with the same key and does not duplicate the invoice', async () => {
    const creds = await enroll('bot-ts-3');
    const t = await task('bot-ts-3', creds, COND_ONE_INVOICE, 'luis@example.com');
    const real = globalThis.fetch;
    let lost = 0;
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      const res = await real(url, init);
      const isInvoice = String(url).endsWith('/invoke')
        && init.body instanceof Uint8Array
        && new TextDecoder().decode(init.body).includes('billing.create_invoice');
      if (isInvoice && lost === 0) {
        lost++;
        await res.arrayBuffer();
        throw new TypeError('fetch failed');
      }
      return res;
    });
    const c = await t.tools.crm.create_customer(
      { name: 'Luis', email: 'luis@example.com' });
    const f = await t.tools.billing.create_invoice(
      { customer_id: (c.data as { id: string }).id, amount: 100 });
    expect(lost).toBe(1);
    expect(f.ok).toBe(true);
    expect((await t.verify()).status).toBe('met');   // exactly ONE invoice
  });

  it('the same signed request sent twice is rejected the second time', async () => {
    const creds = await enroll('bot-ts-4');
    const t = await task('bot-ts-4', creds, COND_CUSTOMER, 'eva@example.com');
    const path = `/v1/tasks/${t.taskId}`;
    const h = buildAuthHeaders('GET', path, new Uint8Array(0), creds);
    const a = await fetch(base + path, { headers: h });
    const b = await fetch(base + path, { headers: h });
    expect(a.status).toBe(200);
    expect(b.status).toBe(401);
  });
});
