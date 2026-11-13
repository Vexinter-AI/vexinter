import { describe, it, expect } from 'vitest';
import {
  canonicalJson,
  sha256Hex,
  createAgentKeyMaterial,
  generateSequence
} from './index';
import { RemoteCredentials, buildAuthHeaders } from '../identity';

describe('Crypto Utilities', () => {
  it('should produce consistent canonical JSON', () => {
    const obj1 = { z: 1, a: 'test', m: { b: 2, a: 1 } };
    const obj2 = { m: { a: 1, b: 2 }, z: 1, a: 'test' };

    const json1 = canonicalJson(obj1);
    const json2 = canonicalJson(obj2);

    expect(json1).toBe(json2);
    expect(json1).toBe('{"a":"test","m":{"a":1,"b":2},"z":1}');
  });

  it('should compute correct SHA-256 hex hash', () => {
    const hash = sha256Hex('hello world');
    expect(hash).toBe('b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9');
  });

  it('should generate valid Ed25519 key pairs', () => {
    const keys = createAgentKeyMaterial();
    expect(keys.signing_priv).toBeInstanceOf(Uint8Array);
    expect(keys.signing_pub).toBeInstanceOf(Uint8Array);
    expect(keys.signing_priv.length).toBe(32);
    expect(keys.signing_pub.length).toBe(32);
  });

  it('should generate unique sequences', () => {
    const seq1 = generateSequence();
    const seq2 = generateSequence();
    expect(seq2 > seq1).toBe(true);
  });
});

describe('Identity & Authentication', () => {
  it('should build signed request headers', () => {
    const keys = createAgentKeyMaterial();
    const creds = new RemoteCredentials('agent-1', 'inst-1', 'cred-1', keys);

    const body = new TextEncoder().encode(canonicalJson({ foo: 'bar' }));
    const headers = buildAuthHeaders('POST', '/v1/tasks', body, creds);

    expect(headers['X-Vex-Credential']).toBe('cred-1');
    expect(headers['X-Vex-Timestamp']).toBeDefined();
    expect(headers['X-Vex-Sequence']).toBeDefined();
    expect(headers['X-Vex-Signature']).toBeDefined();
    expect(headers['Content-Type']).toBe('application/json');
  });
});