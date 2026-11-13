/**
 * Identity and secure messaging for Vexinter TypeScript SDK
 * Handles credentials, envelopes, encryption, and verification
 *
 * Format compatible with vexinter/identity/envelope.py:
 * - body_hash / ct_hash: SHA-256 in hexadecimal
 * - AAD: canonicalJson({header: <9 fields>, body_hash})
 * - signature: Ed25519 over the canonicalJson of the whole envelope minus `signature`
 * - ts: ISO-8601 with "+00:00" and microseconds (like Python's datetime.isoformat)
 */

import {
  canonicalJson,
  sha256Hex,
  sign,
  verify,
  toBase64,
  fromBase64,
  constantTimeEqual,
  generateSequence,
  generateSafeSequence,
  VexinterError,
  x25519KeyAgreement,
  deriveAesGcmKey,
  aesGcmEncrypt,
  aesGcmDecrypt,
  x25519,
  type AgentKeyMaterial,
  type Credential,
  type Envelope,
  type EnvelopeHeader,
  type Rejection,
  type RejectionReason,
  type MessageAck
} from '../crypto';
import { webcrypto } from 'node:crypto';

/**
 * RemoteCredentials wraps AgentKeyMaterial with credential metadata
 * This is the main credentials class used throughout the SDK
 */
export class RemoteCredentials {
  public readonly agent_id: string;
  public readonly instance_id: string;
  public readonly credential_id: string;
  public readonly keys: AgentKeyMaterial;

  constructor(
    agent_id: string,
    instance_id: string,
    credential_id: string,
    keys: AgentKeyMaterial
  ) {
    this.agent_id = agent_id;
    this.instance_id = instance_id;
    this.credential_id = credential_id;
    this.keys = keys;
  }

  get signing_pub_b64(): string {
    return toBase64(this.keys.signing_pub);
  }

  get encryption_pub_b64(): string {
    return toBase64(this.keys.encryption_pub);
  }

  /**
   * Static helper for base64 encoding (used by runtime)
   */
  static toBase64(bytes: Uint8Array): string {
    return toBase64(bytes);
  }

  /**
   * Rotation must go through the service: the successor public keys have to
   * be registered server-side or every signature is rejected. Use
   * `VexRuntime.rotateCredentials()` instead — this method cannot reach the
   * API on its own.
   */
  async rotate(_retentionSeconds: number = 900): Promise<RemoteCredentials> {
    throw new VexinterError(
      'RemoteCredentials.rotate() cannot register keys server-side; ' +
      'use VexRuntime.rotateCredentials() instead',
      400,
      'USE_RUNTIME_ROTATE'
    );
  }
}

/**
 * Timestamp in the format Python produces with datetime.isoformat():
 * "YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00" (fraction only when there are microseconds).
 */
function toPythonIso(d: Date): string {
  const p = (n: number, w = 2) => String(n).padStart(w, '0');
  const base = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}` +
    `T${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
  const ms = d.getUTCMilliseconds();
  const frac = ms === 0 ? '' : '.' + String(ms).padStart(3, '0') + '000';
  return base + frac + '+00:00';
}

/**
 * Normalizes ts to the canonical form the service uses when signing/verifying:
 * "Z" → "+00:00". The fraction is not reformatted: the service keeps the
 * exact microseconds it received, so the transformation is string-level to
 * avoid losing precision (JS Date only keeps milliseconds).
 */
function normalizeTs(ts: string): string {
  if (ts.endsWith('Z')) return ts.slice(0, -1) + '+00:00';
  // No timezone: assumed UTC
  if (!/[+-]\d{2}:?\d{2}$/.test(ts)) return ts + '+00:00';
  return ts;
}

/**
 * The 9 MessageHeader fields that go into the canonical material.
 * `encryption_pub` (extra field in the TS type) is NOT part of the protocol.
 */
function headerDict(h: EnvelopeHeader): Record<string, unknown> {
  return {
    message_id: h.message_id,
    from_agent: h.from_agent,
    from_instance: h.from_instance,
    from_credential: h.from_credential,
    to_agent: h.to_agent,
    task_id: h.task_id ?? null,
    execution_id: h.execution_id ?? null,
    ts: normalizeTs(h.ts),
    sequence: h.sequence
  };
}

/**
 * Signed material: canonicalJson of the whole envelope minus `signature`,
 * same as `signable_material` in envelope.py (includes `ct`).
 */
function signableMaterial(env: Envelope): Uint8Array {
  return new TextEncoder().encode(canonicalJson({
    header: headerDict(env.header),
    body_hash: env.body_hash,
    eph_pub: env.eph_pub,
    nonce: env.nonce,
    ct: env.ct,
    ct_hash: env.ct_hash
  }));
}

/**
 * AAD (Additional Authenticated Data) for AES-GCM.
 * Same as `_aad` in envelope.py: binds the ciphertext to the real header
 * and the body_hash, so an envelope cannot be transplanted to another context.
 */
function aad(header: EnvelopeHeader, bodyHash: string): Uint8Array {
  return new TextEncoder().encode(
    canonicalJson({ header: headerDict(header), body_hash: bodyHash })
  );
}

/**
 * Create an envelope for secure agent-to-agent messaging
 * §9.5: ephemeral ECDH + HKDF-SHA256 + AES-GCM, signed with Ed25519.
 *
 * `options.sequence` must be the next one for the pair
 * (from_agent → to_agent); the service requires a strictly increasing
 * sequence. If omitted, a time-based value is used (valid only if the pair
 * has never written before).
 */
export async function createEnvelope(
  fromCreds: RemoteCredentials,
  toAgent: string,
  toEncryptionPub: Uint8Array,
  payload: unknown,
  options: {
    taskId?: string | null;
    executionId?: string | null;
    sequence?: number;
  } = {}
): Promise<Envelope> {
  const messageId = crypto.randomUUID();
  const ts = toPythonIso(new Date());
  const seq = options.sequence ?? generateSafeSequence(fromCreds.credential_id);

  const header: EnvelopeHeader = {
    message_id: messageId,
    from_credential: fromCreds.credential_id,
    to_agent: toAgent,
    ts,
    from_agent: fromCreds.agent_id,
    from_instance: fromCreds.instance_id,
    task_id: options.taskId ?? null,
    execution_id: options.executionId ?? null,
    sequence: seq,
    encryption_pub: fromCreds.encryption_pub_b64
  };

  // Canonicalize payload → body_hash (hex, same as Python's sha256_b)
  const body = new TextEncoder().encode(canonicalJson(payload));
  const bodyHash = sha256Hex(body);

  // E2E encryption: ECDH(eph_priv, recipient_pub) → HKDF → AES-GCM with real AAD
  const ephPriv = webcrypto.getRandomValues(new Uint8Array(32));
  const ephPub = x25519.getPublicKey(ephPriv);
  const shared = x25519KeyAgreement(ephPriv, toEncryptionPub);
  const nonce = webcrypto.getRandomValues(new Uint8Array(12));
  const key = deriveAesGcmKey(shared, nonce);
  const ct = aesGcmEncrypt(key, nonce, body, aad(header, bodyHash));

  const envelope: Envelope = {
    header,
    body_hash: bodyHash,
    eph_pub: toBase64(ephPub),
    nonce: toBase64(nonce),
    ct: toBase64(ct),
    ct_hash: sha256Hex(ct),
    signature: ''
  };

  const signature = sign(fromCreds.keys.signing_priv, signableMaterial(envelope));
  return { ...envelope, signature: toBase64(signature) };
}

/**
 * Verify the signature of an envelope (without decrypting).
 * Throws if the signature is not valid.
 */
export function verifyEnvelopeSignature(
  envelope: Envelope,
  senderCredential: Credential
): void {
  const sigValid = verify(
    senderCredential.signing_pub,
    signableMaterial(envelope),
    fromBase64(envelope.signature)
  );
  if (!sigValid) {
    const rejection: Rejection = {
      reason: 'bad_signature',
      detail: 'Envelope signature verification failed'
    };
    throw new Error(`Envelope verification failed: ${rejection.reason}`);
  }
}

/**
 * Verify signature, ct_hash and decrypt an incoming envelope.
 * Equivalent to the service's verify_envelope + decrypt_body + body_hash.
 */
export async function verifyAndDecryptEnvelope(
  envelope: Envelope,
  recipientKeys: AgentKeyMaterial,
  senderCredential: Credential
): Promise<unknown> {
  verifyEnvelopeSignature(envelope, senderCredential);
  return decryptEnvelope(envelope, recipientKeys);
}

/**
 * Verify ct_hash, decrypt E2E and verify body_hash.
 * Throws on any tampering.
 */
export async function decryptEnvelope(
  envelope: Envelope,
  recipientKeys: AgentKeyMaterial
): Promise<unknown> {
  // ct_hash = sha256 hex of the raw ciphertext (with tag)
  const ct = fromBase64(envelope.ct);
  const computedCtHash = sha256Hex(ct);
  if (!constantTimeEqual(
    new TextEncoder().encode(computedCtHash),
    new TextEncoder().encode(envelope.ct_hash.toLowerCase())
  )) {
    throw new Error('ct_hash mismatch');
  }

  // ECDH(recipient_priv, eph_pub) → HKDF → AES-GCM
  const ephPub = fromBase64(envelope.eph_pub);
  const nonce = fromBase64(envelope.nonce);
  const shared = x25519KeyAgreement(recipientKeys.encryption_priv, ephPub);
  const key = deriveAesGcmKey(shared, nonce);

  const pt = aesGcmDecrypt(key, nonce, ct, aad(envelope.header, envelope.body_hash));

  // body_hash = sha256 hex of the plaintext
  const computedBodyHash = sha256Hex(pt);
  if (!constantTimeEqual(
    new TextEncoder().encode(computedBodyHash),
    new TextEncoder().encode(envelope.body_hash.toLowerCase())
  )) {
    const rejection: Rejection = {
      reason: 'tampering',
      detail: 'Body hash does not match after decryption'
    };
    throw new Error(`Decryption verification failed: ${rejection.reason}`);
  }

  return JSON.parse(new TextDecoder().decode(pt));
}

/**
 * Verify an envelope using sender's public credential.
 * Alias of verifyEnvelopeSignature (compatibility).
 */
export function verifyEnvelope(
  envelope: Envelope,
  senderCredential: Credential
): void {
  verifyEnvelopeSignature(envelope, senderCredential);
}

/**
 * Build the signed request headers for Vexinter API
 * §9.4: method|path|hash(body)|ts|seq signed with credential
 */
export function buildAuthHeaders(
  method: string,
  path: string,
  body: Uint8Array,
  credentials: RemoteCredentials,
  adminToken?: string
): Record<string, string> {
  const ts = Date.now() / 1000; // Unix timestamp in seconds
  const seq = generateSequence(credentials.credential_id);
  const bodyHash = sha256Hex(body);

  const material = `${method}|${path}|${bodyHash}|${ts}|${seq}`;
  const signature = sign(credentials.keys.signing_priv, new TextEncoder().encode(material));

  const headers: Record<string, string> = {
    'X-Vex-Credential': credentials.credential_id,
    'X-Vex-Timestamp': String(ts),
    'X-Vex-Sequence': seq.toString(),
    'X-Vex-Signature': toBase64(signature),
    'Content-Type': 'application/json'
  };

  if (adminToken) {
    headers['X-Vex-Admin'] = adminToken;
  }

  return headers;
}

/**
 * Create acknowledgment for received message
 */
export function createAck(envelope: Envelope): MessageAck {
  return {
    message_id: envelope.header.message_id,
    body_hash: envelope.body_hash,
    acked_at: new Date().toISOString()
  };
}
