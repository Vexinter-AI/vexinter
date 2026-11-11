import { describe, expect, it } from 'vitest';
import {
  canonicalJson, createAgentKeyMaterial, deriveSuccessorKeyPair,
} from './index';

describe('declared limits of canonical serialization', () => {
  it('rejects instead of producing a hash different from the service hash', () => {
    expect(() => canonicalJson({ t: new Date() })).toThrow();
    expect(() => canonicalJson({ n: 2 ** 53 })).toThrow();
    expect(() => canonicalJson({ n: 1e21 })).toThrow();
    expect(() => canonicalJson({ n: 1e-7 })).toThrow();
  });
});

describe('key rotation', () => {
  it('successor keys are new and not derived from the previous ones', async () => {
    const a = createAgentKeyMaterial();
    const s1 = await deriveSuccessorKeyPair(a.signing_priv, a.encryption_priv);
    const s2 = await deriveSuccessorKeyPair(a.signing_priv, a.encryption_priv);
    expect(Buffer.from(s1.signing.privateKey).equals(
      Buffer.from(s2.signing.privateKey))).toBe(false);
    expect(Buffer.from(s1.signing.privateKey).equals(
      Buffer.from(a.signing_priv))).toBe(false);
  });
});
