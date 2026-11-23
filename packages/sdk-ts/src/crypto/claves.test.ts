import { createPrivateKey, createPublicKey } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { generateEncryptionKeyPair } from './index';

const PREFIJO_PKCS8_X25519 = Buffer.from('302e020100300506032b656e04220420', 'hex');

describe('encryption keys', () => {
  it('are X25519 and the public key matches the private one', () => {
    const k = generateEncryptionKeyPair();
    expect(k.privateKey.length).toBe(32);
    expect(k.publicKey.length).toBe(32);
    const priv = createPrivateKey({
      key: Buffer.concat([PREFIJO_PKCS8_X25519, Buffer.from(k.privateKey)]),
      format: 'der', type: 'pkcs8',
    });
    expect(priv.asymmetricKeyType).toBe('x25519');
    const pub = createPublicKey(priv).export({ type: 'spki', format: 'der' });
    expect(Buffer.from(pub.subarray(pub.length - 32))
      .equals(Buffer.from(k.publicKey))).toBe(true);
  });
});
