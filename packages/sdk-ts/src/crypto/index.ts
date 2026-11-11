/**
 * Cryptographic utilities for Vexinter TypeScript SDK
 * Uses @noble/ed25519 and @noble/hashes for Ed25519 and SHA-256
 * Uses @noble/curves for X25519 ECDH
 * Uses Node.js crypto for AES-GCM and random bytes
 */

import * as ed25519 from '@noble/ed25519';
import { sha256 } from '@noble/hashes/sha256';
import { sha512 } from '@noble/hashes/sha512';
import { hkdf } from '@noble/hashes/hkdf';
import { x25519 } from '@noble/curves/ed25519';
import { createCipheriv, createDecipheriv, generateKeyPairSync, randomBytes, randomUUID, webcrypto } from 'node:crypto';
import {
  VexinterError,
  RejectionError,
  RemoteError,
  type RejectionReason,
  type AgentKeyMaterial,
  type Credential,
  type Envelope,
  type EnvelopeHeader,
  type Rejection,
  type MessageAck
} from '../types/index';

// Setup sha512Sync for @noble/ed25519
ed25519.etc.sha512Sync = (...m: Uint8Array[]) => sha512(ed25519.etc.concatBytes(...m));

// Re-export x25519 from @noble/curves for use in other modules
export { x25519 };

// Re-export error classes (defined in types) - as values
export { VexinterError, RejectionError, RemoteError };
export type { RejectionReason, AgentKeyMaterial, Credential, Envelope, EnvelopeHeader, Rejection, MessageAck };

// Exported under a non-colliding name: `crypto` would shadow the Web Crypto
// API global for any consumer doing `import { crypto } from '@vexinter-ai/sdk'`.
export const vexCrypto = {
  randomUUID,
  deriveSuccessorKeyPair,
};

export const GENESIS = new Uint8Array(32).fill(0);

function compareCodePoints(a: string, b: string): number {
  const ca = Array.from(a);
  const cb = Array.from(b);
  const n = Math.min(ca.length, cb.length);
  for (let i = 0; i < n; i++) {
    const x = ca[i].codePointAt(0) as number;
    const y = cb[i].codePointAt(0) as number;
    if (x !== y) return x - y;
  }
  return ca.length - cb.length;
}

/**
 * Canonical serialization compatible with Python's `canonical_json`:
 * keys sorted by Unicode code point, no whitespace, unescaped UTF-8.
 *
 * Limits (throws instead of producing a hash that differs from the
 * service's): dates (convert to ISO text first), integers outside the
 * safe range (use bigint or text), and scientific-notation numbers.
 * JavaScript does not distinguish 100 from 100.0 and Python writes
 * "100.0" for a float: amounts must travel as integers or text.
 */
export function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) throw new Error('undefined is not valid in canonical JSON');
  if (typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('Non-finite numbers not allowed');
    if (Number.isInteger(value)) {
      if (!Number.isSafeInteger(value)) {
        throw new Error('integer outside safe range: use bigint or text');
      }
      return String(value);
    }
    const t = String(value);
    if (t.includes('e')) {
      throw new Error('scientific notation: Python and JavaScript write it differently');
    }
    return t;
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (value instanceof Date) {
    throw new Error('dates: convert to ISO text before serializing');
  }
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalJson).join(',') + ']';
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => compareCodePoints(a, b))
      .map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`);
    return '{' + entries.join(',') + '}';
  }
  throw new Error(`Unsupported type: ${typeof value}`);
}

/**
 * SHA-256 hash as hex string
 */
export function sha256Hex(data: Uint8Array | string): string {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  return Buffer.from(sha256(bytes)).toString('hex');
}

/**
 * SHA-256 hash as bytes
 */
export function sha256Bytes(data: Uint8Array | string): Uint8Array {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  return sha256(bytes);
}

/**
 * Generate a new Ed25519 key pair for signing
 */
export function generateSigningKeyPair(): { privateKey: Uint8Array; publicKey: Uint8Array } {
  const privateKey = ed25519.utils.randomPrivateKey();
  const publicKey = ed25519.getPublicKey(privateKey);
  return { privateKey, publicKey };
}

/**
 * X25519 key pair for encryption: the same key type the service uses.
 * The 32 bytes come from Node's DER export.
 */
export function generateEncryptionKeyPair(): { privateKey: Uint8Array; publicKey: Uint8Array } {
  const { publicKey, privateKey } = generateKeyPairSync('x25519');
  const pub = publicKey.export({ type: 'spki', format: 'der' });
  const priv = privateKey.export({ type: 'pkcs8', format: 'der' });
  return {
    privateKey: new Uint8Array(priv.subarray(priv.length - 32)),
    publicKey: new Uint8Array(pub.subarray(pub.length - 32)),
  };
}

/**
 * Sign a message with Ed25519 private key
 */
export function sign(privateKey: Uint8Array, message: Uint8Array): Uint8Array {
  return ed25519.sign(message, privateKey);
}

/**
 * Verify an Ed25519 signature
 */
export function verify(publicKey: Uint8Array, message: Uint8Array, signature: Uint8Array): boolean {
  try {
    return ed25519.verify(signature, message, publicKey);
  } catch {
    return false;
  }
}

/**
 * Successor keys for rotation (§9.3): generated AT RANDOM. They are not
 * derived from the previous ones: if a key is compromised, its successor
 * cannot be compromised along with it.
 */
export async function deriveSuccessorKeyPair(
  _currentSigningPriv?: Uint8Array,
  _currentEncryptionPriv?: Uint8Array
): Promise<{ signing: { privateKey: Uint8Array; publicKey: Uint8Array }; encryption: { privateKey: Uint8Array; publicKey: Uint8Array } }> {
  return { signing: generateSigningKeyPair(), encryption: generateEncryptionKeyPair() };
}

// Monotonicity is tracked PER SIGNING IDENTITY (credential_id), not in a
// single process-wide counter: sequences of different agents must not
// share state in multi-agent processes.
const _lastSeqByKey = new Map<string, bigint>();

/**
 * Generate a unique sequence number for anti-replay
 * Combines timestamp (high bits) with random bits (low bits).
 * `key` should be the credential_id so each agent keeps its own counter.
 */
export function generateSequence(key = 'default'): bigint {
  const ts = BigInt(Date.now()) << 20n;
  const random = BigInt('0x' + randomBytes(3).toString('hex')) & 0xfffffn; // 20 bits, same as Python
  let seq = ts | random;
  const last = _lastSeqByKey.get(key) ?? 0n;
  if (seq <= last) {
    seq = last + 1n;
  }
  _lastSeqByKey.set(key, seq);
  return seq;
}

/**
 * Largest sequence that survives a JSON number round-trip exactly
 * (Number.MAX_SAFE_INTEGER). generateSequence() exceeds it, so callers
 * that must serialize `sequence` as a JSON number use this mask: the low
 * 53 bits still carry ~33 bits of millisecond timestamp plus the 20-bit
 * random part, staying monotonic per credential via _lastSeqByKey.
 */
export const SEQUENCE_SAFE_MASK = 0x1fffffffffffffn; // 2^53 - 1
export function generateSafeSequence(key?: string): number {
  return Number(generateSequence(key) & SEQUENCE_SAFE_MASK);
}

/**
 * Generate a unique idempotency key
 */
export function generateIdempotencyKey(): string {
  return randomBytes(16).toString('hex');
}

/**
 * Standard base64 WITH padding: the same format the service uses
 * (b64e/b64d in identity/service.py).
 */
export function toBase64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString('base64');
}

/**
 * Decodes standard base64; also accepts the URL-safe variant and
 * unpadded input.
 */
export function fromBase64(b64: string): Uint8Array {
  const std = b64.replace(/-/g, '+').replace(/_/g, '/');
  return new Uint8Array(Buffer.from(std, 'base64'));
}

/**
 * Create a new AgentKeyMaterial with fresh keys
 */
export function createAgentKeyMaterial(): AgentKeyMaterial {
  const { privateKey: signing_priv, publicKey: signing_pub } = generateSigningKeyPair();
  const { privateKey: encryption_priv, publicKey: encryption_pub } = generateEncryptionKeyPair();

  return {
    signing_priv,
    signing_pub,
    encryption_priv,
    encryption_pub
  };
}

/**
 * Serialize AgentKeyMaterial for storage/transmission
 */
export function serializeKeyMaterial(keys: AgentKeyMaterial): string {
  return JSON.stringify({
    signing_priv: toBase64(keys.signing_priv),
    signing_pub: toBase64(keys.signing_pub),
    encryption_priv: toBase64(keys.encryption_priv),
    encryption_pub: toBase64(keys.encryption_pub),
    credential_id: keys.credential_id,
    instance_id: keys.instance_id,
    agent_id: keys.agent_id,
    expires_at: keys.expires_at,
    grace_until: keys.grace_until
  });
}

/**
 * Deserialize AgentKeyMaterial from storage/transmission
 */
export function deserializeKeyMaterial(json: string): AgentKeyMaterial {
  const data = JSON.parse(json);
  const keys: AgentKeyMaterial = {
    signing_priv: fromBase64(data.signing_priv),
    signing_pub: fromBase64(data.signing_pub),
    encryption_priv: fromBase64(data.encryption_priv),
    encryption_pub: fromBase64(data.encryption_pub),
    credential_id: data.credential_id,
    instance_id: data.instance_id,
    agent_id: data.agent_id,
    expires_at: data.expires_at,
    grace_until: data.grace_until
  };

  // Integrity checks: wrong lengths or mismatched pairs mean the material
  // was tampered with or corrupted — reject instead of failing later
  // with an opaque signature/decryption error.
  const eq = (a: Uint8Array, b: Uint8Array) =>
    a.length === b.length && a.every((v, i) => v === b[i]);
  if (keys.signing_priv.length !== 32 || keys.signing_pub.length !== 32 ||
      keys.encryption_priv.length !== 32 || keys.encryption_pub.length !== 32) {
    throw new VexinterError('invalid key material: keys must be 32 bytes each', 422, 'INVALID_KEY_MATERIAL');
  }
  if (!eq(ed25519.getPublicKey(keys.signing_priv), keys.signing_pub)) {
    throw new VexinterError('invalid key material: signing pub does not match priv', 422, 'INVALID_KEY_MATERIAL');
  }
  if (!eq(x25519.getPublicKey(keys.encryption_priv), keys.encryption_pub)) {
    throw new VexinterError('invalid key material: encryption pub does not match priv', 422, 'INVALID_KEY_MATERIAL');
  }
  // Effective validity mirrors Python's Credential.valid_until:
  // min(expires_at, grace_until) — a rotated-out key dies at grace end
  // even though its raw expires_at is still in the future.
  const expiry = [keys.expires_at, keys.grace_until]
    .filter((v): v is string => !!v)
    .map(Date.parse)
    .filter((t) => !Number.isNaN(t));
  if (expiry.length && Math.min(...expiry) <= Date.now()) {
    throw new VexinterError('invalid key material: expired', 422, 'INVALID_KEY_MATERIAL');
  }
  return keys;
}

/**
 * Content fingerprint, same as Python's `hash_payload`: null is treated
 * as an empty object.
 */
export function hashPayload(payload: unknown): string {
  return sha256Hex(canonicalJson(payload === null || payload === undefined ? {} : payload));
}

/**
 * Event hash, same as `this_hash` on the service:
 * sha256(canonical_json(hash_material)), with hash_material = id, stream, seq,
 * type, ts, task_id, execution_id, agent_id, operation_id, caused_by,
 * depends_on, compensates, payload_hash and prev_hash.
 * `ts` is the text the service uses to compute the hash (ISO with "+00:00");
 * if it arrives with "Z" (as in API JSON responses) it is normalized.
 * NOT VERIFIED against real API responses with fractional seconds.
 */
export function computeEventHash(
  prevHash: string,
  payloadHash: string,
  eventData: Record<string, unknown>
): string {
  const keys = ['id', 'stream', 'seq', 'type', 'ts', 'task_id', 'execution_id',
    'agent_id', 'operation_id', 'caused_by', 'depends_on', 'compensates'];
  const material: Record<string, unknown> = {};
  for (const k of keys) {
    const v = eventData[k];
    material[k] = v === undefined ? (k === 'depends_on' ? [] : null) : v;
  }
  const ts = material['ts'];
  if (typeof ts === 'string') material['ts'] = ts.replace(/Z$/, '+00:00');
  material['payload_hash'] = payloadHash;
  material['prev_hash'] = prevHash;
  return sha256Hex(canonicalJson(material));
}

/**
 * Constant-time comparison for secrets (tokens, signatures)
 */
export function constantTimeEqual(a: Uint8Array | string, b: Uint8Array | string): boolean {
  const aa = typeof a === 'string' ? new TextEncoder().encode(a) : a;
  const bb = typeof b === 'string' ? new TextEncoder().encode(b) : b;

  // Hash both sides first: the comparison runs on fixed-length digests,
  // so neither the input length nor JIT-friendly early exits leak timing.
  const da = sha256Bytes(aa);
  const db = sha256Bytes(bb);
  let diff = aa.length === bb.length ? 0 : 1;
  for (let i = 0; i < da.length; i++) {
    diff |= da[i] ^ db[i];
  }
  return diff === 0;
}

const KDF_INFO = new TextEncoder().encode('vexinter-envelope-v1');

/**
 * X25519 ECDH key agreement
 */
export function x25519KeyAgreement(priv: Uint8Array, pub: Uint8Array): Uint8Array {
  return x25519.getSharedSecret(priv, pub);
}

/**
 * Derive AES-GCM key from ECDH shared secret using HKDF-SHA256
 * salt = nonce (12 bytes), info = "vexinter-envelope-v1"
 */
export function deriveAesGcmKey(sharedSecret: Uint8Array, nonce: Uint8Array): Uint8Array {
  return hkdf(sha256, sharedSecret, nonce, KDF_INFO, 32);
}

/**
 * AES-GCM encrypt with 12-byte nonce using Node.js crypto
 * Returns ciphertext with auth tag appended
 */
export function aesGcmEncrypt(key: Uint8Array, nonce: Uint8Array, plaintext: Uint8Array, aad: Uint8Array): Uint8Array {
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(aad);
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const authTag = cipher.getAuthTag();
  return new Uint8Array(Buffer.concat([ciphertext, authTag]));
}

/**
 * AES-GCM decrypt with 12-byte nonce using Node.js crypto
 * ciphertext must include auth tag at the end (16 bytes)
 */
export function aesGcmDecrypt(key: Uint8Array, nonce: Uint8Array, ciphertext: Uint8Array, aad: Uint8Array): Uint8Array {
  if (ciphertext.length < 16) {
    throw new Error('Ciphertext too short for AES-GCM');
  }
  const authTag = ciphertext.subarray(ciphertext.length - 16);
  const ct = ciphertext.subarray(0, ciphertext.length - 16);
  const decipher = createDecipheriv('aes-256-gcm', key, nonce);
  decipher.setAAD(aad);
  decipher.setAuthTag(Buffer.from(authTag));
  const plaintext = Buffer.concat([decipher.update(ct), decipher.final()]);
  return new Uint8Array(plaintext);
}

/**
 * Generate a new X25519 key pair for encryption (using @noble/curves)
 */
export function generateEncryptionKeyPairNoble(): { privateKey: Uint8Array; publicKey: Uint8Array } {
  const privateKey = x25519.utils.randomPrivateKey();
  const publicKey = x25519.getPublicKey(privateKey);
  return { privateKey, publicKey };
}