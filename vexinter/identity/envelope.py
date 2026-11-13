from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel

from ..core.crypto import canonical_json
from ..core.events import new_id, now
from .service import b64d, b64e

_KDF_INFO = b"vexinter-envelope-v1"


def sha256_b(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class MessageHeader(BaseModel):
    """Authenticated message metadata (§9.4).

    `sequence` is the anti-replay window: strictly monotonic per
    (logical sender → logical recipient).
    """
    message_id: str
    from_agent: str
    from_instance: str
    from_credential: str
    to_agent: str
    task_id: str | None = None         
    execution_id: str | None = None
    ts: datetime
    sequence: int


class Envelope(BaseModel):
    """End-to-end signed and encrypted envelope.

    - `body_hash`: sha256 of the plaintext (verifiable commitment; does not
      reveal the content because the hash is preimage resistant).
    - `ct_hash`: sha256 of the ciphertext, covered by the signature → any
      manipulation of the ciphertext invalidates the signature BEFORE
      decrypting.
    - `signature`: sender's Ed25519 over header + body_hash + eph_pub +
      nonce + ct_hash (the whole envelope except the signature itself,
      including the ciphertext).
    """
    header: MessageHeader
    body_hash: str
    eph_pub: str        
    nonce: str      
    ct: str            
    ct_hash: str
    signature: str


def signable_material(env: Envelope) -> bytes:
    return canonical_json(env.model_dump(exclude={"signature"})).encode()


def _aad(header: MessageHeader, body_hash: str) -> bytes:
    """Additional authenticated data: the ciphertext is cryptographically
    bound to from/to/seq/message_id/ts/task. An envelope cannot be
    transplanted to another context (§9.4)."""
    return canonical_json(
        {"header": header.model_dump(), "body_hash": body_hash}).encode()


class AgentKeyMaterial:
    """AGENT-side keys (not infrastructure keys).

    The signing private key and the decryption key live ONLY here (§14).
    Vexinter only knows the public versions, registered when the credential
    is issued.

    Rotation (§9.3): `successor()` generates new keys and retains the
    previous DECRYPTION keys for a grace period, so that messages in flight
    encrypted to the old key can still be opened. The previous signing key
    is not retained: after rotation, signing uses the new one. Once the
    grace period ends, the old decryption key is discarded.
    """

    def __init__(self) -> None:
        self._sign = Ed25519PrivateKey.generate()
        self._enc = X25519PrivateKey.generate()
        self._previous: list[tuple[X25519PrivateKey, float]] = []

    @property
    def signing_pub_b64(self) -> str:
        return b64e(self._sign.public_key().public_bytes_raw())

    @property
    def encryption_pub_b64(self) -> str:
        return b64e(self._enc.public_key().public_bytes_raw())

    @property
    def signing_priv_b64(self) -> str:
        """Signing private key in base64 (local persistence only, §14)."""
        return b64e(self._sign.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption()))

    @property
    def encryption_priv_b64(self) -> str:
        """Decryption private key in base64 (local persistence only, §14)."""
        return b64e(self._enc.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption()))

    @classmethod
    def from_private_b64(cls, signing_priv_b64: str,
                         encryption_priv_b64: str,
                         previous: list[dict] | None = None
                         ) -> AgentKeyMaterial:
        """Restores keys from their serialized form (e.g. IdentityPersistence).

        `previous` carries the grace-period decryption keys as
        [{"key": <b64>, "expires_at": <epoch>}]; expired entries are
        dropped on load, same as _decryption_keys() does at runtime.
        """
        km = cls.__new__(cls)
        km._sign = Ed25519PrivateKey.from_private_bytes(b64d(signing_priv_b64))
        km._enc = X25519PrivateKey.from_private_bytes(b64d(encryption_priv_b64))
        km._previous = []
        t = time.time()
        for item in previous or []:
            try:
                k = X25519PrivateKey.from_private_bytes(b64d(item["key"]))
                exp = float(item["expires_at"])
            except Exception as e:  # noqa: BLE001 - corrupt entry: skip it
                import logging
                logging.getLogger(__name__).debug(
                    "skipping corrupt previous key entry: %s", e)
                continue
            if exp > t:
                km._previous.append((k, exp))
        return km

    def previous_keys_serialized(self) -> list[dict]:
        """Grace-period decryption keys in persistable form."""
        return [{
            "key": b64e(k.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption())),
            "expires_at": h,
        } for k, h in self._previous]

    def sign(self, data: bytes) -> str:
        return b64e(self._sign.sign(data))

    def successor(self, grace_s: float = 900.0) -> AgentKeyMaterial:
        """New keys that inherit, for `grace_s` seconds, the decryption keys
        of this one (and any previous ones still valid)."""
        nuevo = AgentKeyMaterial()
        t = time.time()
        nuevo._previous = ([(self._enc, t + grace_s)]
                           + [(k, h) for k, h in self._previous if h > t])
        return nuevo

    def _decryption_keys(self) -> list[X25519PrivateKey]:
        t = time.time()
        self._previous = [(k, h) for k, h in self._previous if h > t]
        return [self._enc] + [k for k, _ in self._previous]

    @staticmethod
    def _open(enc_key: X25519PrivateKey, env: Envelope) -> bytes:
        """E2E decryption: ECDH(private key, ephemeral public) → HKDF → AES-GCM."""
        shared = enc_key.exchange(
            X25519PublicKey.from_public_bytes(b64d(env.eph_pub)))
        key = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=b64d(env.nonce), info=_KDF_INFO).derive(shared)
        return AESGCM(key).decrypt(b64d(env.nonce), b64d(env.ct),
                                   _aad(env.header, env.body_hash))

    def decrypt_body(self, env: Envelope) -> bytes:
        ultimo: InvalidTag | None = None
        for k in self._decryption_keys():
            try:
                return self._open(k, env)
            except InvalidTag as e:
                ultimo = e
        if ultimo is None:
            raise InvalidTag("no decryption keys")
        raise ultimo


def build_envelope_for(recipient_enc_pub_b64: str, *, sender_keys,
                       from_agent: str, from_instance: str, credential_id: str,
                       to_agent: str, plaintext, sequence: int,
                       task_id: str | None = None,
                       execution_id: str | None = None) -> Envelope:
    """Core construction: valid embedded or against the service.

    The remote side obtains the recipient's public key over HTTP; the
    sender's private key never leaves the client (§14).
    """
    pt = plaintext.encode() if isinstance(plaintext, str) else plaintext
    nonce = os.urandom(12)
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(
        X25519PublicKey.from_public_bytes(b64d(recipient_enc_pub_b64)))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce,
               info=_KDF_INFO).derive(shared)
    body_hash = sha256_b(pt)
    header = MessageHeader(
        message_id=new_id("msg"), from_agent=from_agent,
        from_instance=from_instance, from_credential=credential_id,
        to_agent=to_agent, task_id=task_id, execution_id=execution_id,
        ts=now(), sequence=sequence)
    ct = AESGCM(key).encrypt(nonce, pt, _aad(header, body_hash))
    env = Envelope(header=header, body_hash=body_hash,
                  eph_pub=b64e(eph.public_key().public_bytes_raw()),
                  nonce=b64e(nonce), ct=b64e(ct), ct_hash=sha256_b(ct),
                  signature="")
    env.signature = sender_keys.sign(signable_material(env))
    return env


def build_envelope(*, identity_service, sender_keys, credential_id, to_agent,
                   plaintext, sequence, task_id=None, execution_id=None):
    """Embedded variant: validates the credential and resolves the public key."""
    cred = identity_service.assert_credential_valid(credential_id)
    enc_pub = identity_service.encryption_pub_for_agent(to_agent)
    return build_envelope_for(enc_pub, sender_keys=sender_keys,
                              from_agent=cred.agent_id,
                              from_instance=cred.instance_id,
                              credential_id=credential_id, to_agent=to_agent,
                              plaintext=plaintext, sequence=sequence,
                              task_id=task_id, execution_id=execution_id)