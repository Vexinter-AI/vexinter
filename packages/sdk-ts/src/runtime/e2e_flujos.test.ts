// Key rotation, per-task collaborators and human decisions, against the REAL
// Python service (not a mocked fetch).
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { createAgentKeyMaterial } from '../crypto';
import { ADMIN, hasService, probeError, startService, type Service } from '../../test-utils/e2e';
import { AsyncHttpClient, invokeTool, retryConfig } from './client';
import { AsyncRemoteRuntime } from './runtime';

retryConfig.baseMs = 1;
vi.setConfig({ testTimeout: 30_000, hookTimeout: 45_000 });

if (!hasService && process.env.VEXINTER_E2E_REQUIRED) {
  describe('SDK flows against the real Python service', () => {
    it('the Python environment with Vexinter is required', () => {
      throw new Error('VEXINTER_E2E_REQUIRED is set but there is no Python environment with Vexinter\nprobe: ' + probeError);
    });
  });
}

type Creds = Awaited<ReturnType<AsyncRemoteRuntime['issueCredential']>>;
type Ev = { type: string; payload?: Record<string, unknown> };

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

function helpers(base: string) {
  const enroll = async (agent: string): Promise<Creds> => {
    const adm = new AsyncRemoteRuntime({ endpoint: base, adminToken: ADMIN });
    await adm.enrollAgent(agent,
      { allowedTools: ['crm.create_customer', 'billing.create_invoice'] });
    const inst = await adm.spawnInstance(agent);
    return adm.issueCredential(inst.instance_id, createAgentKeyMaterial());
  };
  const task = async (agent: string, creds: Creds, condition: unknown, email: string) => {
    const rt = new AsyncRemoteRuntime({ endpoint: base, credentials: creds });
    return rt.task({ agent, objective: 'Test from TypeScript',
                     condition, input: { email }, credentials: creds }).enter();
  };
  return { enroll, task };
}

describe.skipIf(!hasService)('key rotation and collaborators (real service)', () => {
  let svc: Service;
  let base = '';
  beforeAll(async () => { svc = await startService(); base = svc.base; });
  afterAll(() => svc?.stop());

  it('the rotated credential signs, the previous one works during grace and a replaced one cannot rotate again', async () => {
    const { enroll, task } = helpers(base);
    const old = await enroll('bot-rot');
    const rt = new AsyncRemoteRuntime({ endpoint: base, credentials: old });
    const fresh = await rt.rotateCredentials(old);
    expect(fresh.credential_id).not.toBe(old.credential_id);
    const t1 = await task('bot-rot', fresh, COND_CUSTOMER, 'rot1@example.com');
    await t1.exit();
    const t2 = await task('bot-rot', old, COND_CUSTOMER, 'rot2@example.com');
    await t2.exit();
    await expect(rt.rotateCredentials(old)).rejects.toMatchObject({ statusCode: 422 });
  });

  it('a foreign agent cannot invoke on someone else\'s task until the owner authorizes it', async () => {
    const { enroll, task } = helpers(base);
    const a = await enroll('bot-owner');
    const b = await enroll('bot-collab');
    const t = await task('bot-owner', a, COND_CUSTOMER, 'collab@example.com');
    const http = new AsyncHttpClient({ endpoint: base });
    await expect(invokeTool(http, t.taskId!, 'crm.create_customer',
      { name: 'X', email: 'collab@example.com' }, b)).rejects.toMatchObject({ statusCode: 403 });
    const r = await t.callAs(b, 'crm.create_customer',
      { name: 'Ana', email: 'collab@example.com' });
    expect(r.ok).toBe(true);
    const evs = (await t.events()) as unknown as Ev[];
    expect(evs.some((e) => e.type === 'tool.invoked'
      && (e.payload?.invoked_by as { agent_id?: string } | undefined)?.agent_id === 'bot-collab'))
      .toBe(true);
    await t.exit();
  });

  it('only the task owner adds collaborators', async () => {
    const { enroll, task } = helpers(base);
    const a = await enroll('bot-owner2');
    const b = await enroll('bot-intruder');
    const t = await task('bot-owner2', a, COND_CUSTOMER, 'intruder@example.com');
    const http = new AsyncHttpClient({ endpoint: base });
    await expect(http.request('POST', `/v1/tasks/${t.taskId}/collaborators`,
      { agent: 'bot-intruder' }, { credentials: b })).rejects.toMatchObject({ statusCode: 403 });
    await t.exit();
  });
});

describe.skipIf(!hasService)('human decision (real service with a fake success)', () => {
  let svc: Service;
  let base = '';
  beforeAll(async () => { svc = await startService(['fail']); base = svc.base; });
  afterAll(() => svc?.stop());

  it('a fake success ends in a case only the admin can resolve, attributed to admin', async () => {
    const { enroll, task } = helpers(base);
    const creds = await enroll('bot-case');
    const t = await task('bot-case', creds, COND_ONE_INVOICE, 'case@example.com');
    const c = await t.tools.crm.create_customer({ name: 'Case', email: 'case@example.com' });
    const f = await t.tools.billing.create_invoice(
      { customer_id: (c.data as { id: string }).id, amount: 100 });
    expect(f.ok).toBe(true);                                   // the fake success
    expect((await t.verify()).status).toBe('not_met');
    const rec = await t.recover('billing.create_invoice', f.provenance.invoked_event);
    expect(rec).not.toBeNull();
    const id = (rec as unknown as { id: string }).id;

    const adm = new AsyncRemoteRuntime({ endpoint: base, adminToken: ADMIN });
    const rows = (await adm.pendingCases()) as unknown as
      Array<{ case: { id: string }; options: Array<{ action: string }> }>;
    const mine = rows.find((x) => x.case.id === id);
    expect(mine).toBeDefined();
    expect(mine!.options.map((o) => o.action)).toContain('retry');

    const rtAgent = new AsyncRemoteRuntime({ endpoint: base, credentials: creds });
    await expect(rtAgent.decide(id, 'close')).rejects.toMatchObject({ statusCode: 403 });

    const v = (await adm.decide(id, 'retry', 'someone-else')) as { status: string };
    expect(v.status).toBe('met');
    expect((await t.verify()).status).toBe('met');
    const evs = (await t.events()) as unknown as Ev[];
    const dec = evs.find((e) => e.type === 'recovery.human_decided');
    expect(dec?.payload?.operator).toBe('admin');   // the server ignores the client-supplied name
    await t.exit();
  });
});
