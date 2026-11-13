import { describe, expect, it } from 'vitest';
import { createAgentKeyMaterial, toBase64 } from '../crypto';
import { RemoteCredentials, createEnvelope, verifyAndDecryptEnvelope } from './index';
import type { AgentKeyMaterial, Credential } from '../types';

function credentialFor(keys: AgentKeyMaterial, agent: string, instance: string, cred: string): Credential {
  return {
    credential_id: cred,
    agent_id: agent,
    instance_id: instance,
    signing_pub: keys.signing_pub,
    encryption_pub: keys.encryption_pub,
    issued_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 3600_000).toISOString(),
  };
}

describe('envelopes', () => {
  it('real encryption: the recipient verifies the signature and decrypts the body', async () => {
    const sender = createAgentKeyMaterial();
    const recipient = createAgentKeyMaterial();
    const creds = new RemoteCredentials('a', 'inst', 'cred', sender);
    const env = await createEnvelope(creds, 'b', recipient.encryption_pub, { x: 1 }, { sequence: 1 });

    // body_hash and ct_hash are SHA-256 hex (Python service format)
    expect(env.body_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(env.ct_hash).toMatch(/^[0-9a-f]{64}$/);

    const pt = await verifyAndDecryptEnvelope(
      env, recipient, credentialFor(sender, 'a', 'inst', 'cred'));
    expect(pt).toEqual({ x: 1 });
  });

  it('rejects a tampered envelope', async () => {
    const sender = createAgentKeyMaterial();
    const recipient = createAgentKeyMaterial();
    const creds = new RemoteCredentials('a', 'inst', 'cred', sender);
    const env = await createEnvelope(creds, 'b', recipient.encryption_pub, { x: 1 }, { sequence: 1 });

    const tampered = { ...env, ct: env.ct.slice(0, -4) + 'AAAA' };
    await expect(verifyAndDecryptEnvelope(
      tampered, recipient, credentialFor(sender, 'a', 'inst', 'cred')))
      .rejects.toThrow();
  });
});
