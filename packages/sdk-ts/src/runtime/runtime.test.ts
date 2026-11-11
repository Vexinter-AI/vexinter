import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RejectionError, createAgentKeyMaterial, verify } from '../crypto';
import { RemoteCredentials } from '../identity';
import { AsyncHttpClient, invokeTool, retryConfig } from './client';
import { AsyncRemoteRuntime } from './runtime';

if (retryConfig) retryConfig.baseMs = 1;   // retries without long waits in tests

const keys = createAgentKeyMaterial();
const creds = new RemoteCredentials('bot', 'inst_1', 'cred_1', keys);

type Call = { url: string; method: string; headers: Record<string, string>; body?: Uint8Array };

const json = (obj: unknown, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json' } });
const dec = (b?: Uint8Array) => JSON.parse(new TextDecoder().decode(b));

function mock(responses: Array<() => Response>): Call[] {
  const calls: Call[] = [];
  let i = 0;
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({
      url: String(url), method: String(init.method),
      headers: { ...(init.headers as Record<string, string>) },
      body: init.body as Uint8Array | undefined,
    });
    return responses[Math.min(i++, responses.length - 1)]();
  });
  return calls;
}

const task = () => {
  const rt = new AsyncRemoteRuntime({ endpoint: 'http://x', credentials: creds });
  return rt.task({ agent: 'bot', objective: 'o', condition: {}, credentials: creds });
};
const create = () => json({ task_id: 't1', execution_id: 'e1' });

afterEach(() => vi.unstubAllGlobals());

describe('async client against the service (mocked fetch)', () => {
  it('t.tools.system.tool invokes the tool', async () => {
    const calls = mock([create, () => json({ ok: true, data: { id: 'c1' }, provenance: {} })]);
    const t = await task().enter();
    const r = await t.tools.crm.create_customer({ name: 'Ana' });
    expect(r.ok).toBe(true);
    expect(calls[1].url).toContain('/v1/tasks/t1/invoke');
    expect(dec(calls[1].body).tool).toBe('crm.create_customer');
  });

  it('GET queries carry no body', async () => {
    const calls = mock([create, () => json({ state: 'satisfied' })]);
    const t = await task().enter();
    expect(await t.getState()).toBe('satisfied');
    expect(calls[1].method).toBe('GET');
    expect(calls[1].body).toBeUndefined();
  });

  it('a transport retry signs again and keeps the idempotency key', async () => {
    const calls = mock([
      () => { throw new TypeError('fetch failed'); },
      () => json({ ok: true, data: {}, provenance: {} }),
    ]);
    const http = new AsyncHttpClient({ endpoint: 'http://x', credentials: creds });
    await invokeTool(http, 't1', 'crm.create_customer', { name: 'Ana' }, creds);
    expect(calls.length).toBe(2);
    expect(calls[0].headers['X-Vex-Sequence']).not.toBe(calls[1].headers['X-Vex-Sequence']);
    expect(dec(calls[0].body).idempotency_key).toBe(dec(calls[1].body).idempotency_key);
  });

  it('the request signature verifies with the public key and uses standard base64', async () => {
    const calls = mock([() => json({ ok: true, data: {}, provenance: {} })]);
    const http = new AsyncHttpClient({ endpoint: 'http://x', credentials: creds });
    await invokeTool(http, 't1', 'crm.x', { a: 1 }, creds);
    const h = calls[0].headers;
    const material = `POST|/v1/tasks/t1/invoke|${createHash('sha256').update(calls[0].body!).digest('hex')}|${h['X-Vex-Timestamp']}|${h['X-Vex-Sequence']}`;
    const sig = Buffer.from(h['X-Vex-Signature'], 'base64');
    expect(sig.length).toBe(64);
    expect(sig.toString('base64')).toBe(h['X-Vex-Signature']);
    expect(verify(keys.signing_pub, new TextEncoder().encode(material), new Uint8Array(sig))).toBe(true);
    expect(h['X-Vex-Credential']).toBe('cred_1');
  });

  it('recover() with no arguments sends nulls instead of failing', async () => {
    const calls = mock([create, () => json({ case: null })]);
    const t = await task().enter();
    expect(await t.recover()).toBeNull();
    expect(dec(calls[1].body)).toEqual({ failed_tool: null, invoked_event: null });
  });

  it('a 422 with a reason becomes a RejectionError', async () => {
    mock([() => json({ detail: { reason: 'replay', detail: 'already seen' } }, 422)]);
    const http = new AsyncHttpClient({ endpoint: 'http://x', credentials: creds });
    await expect(invokeTool(http, 't1', 'crm.x', {}, creds)).rejects.toBeInstanceOf(RejectionError);
  });

  it('pendingCases and caseOptions read the real {case, options} shape', async () => {
    const rows = [
      { case: { id: 'case1', task_id: 't1' }, options: [{ action: 'retry' }] },
      { case: { id: 'case2', task_id: 'other' }, options: [] },
    ];
    mock([create, () => json(rows), () => json(rows)]);
    const t = await task().enter();
    expect((await t.pendingCases()).map((c) => c.id)).toEqual(['case1']);
    expect(await t.caseOptions('case1')).toEqual([{ action: 'retry' }]);
  });
});
