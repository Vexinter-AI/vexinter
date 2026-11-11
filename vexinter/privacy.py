"""Privacy (§16): per-execution encryption and cryptographic erasure.

The store is append-only and hash-chained, so content cannot be "edited"
to erase it. Instead, the personal content of each event is encrypted
with a key specific to the EXECUTION, BEFORE entering the store; the
hash chain covers the ciphertext. Erasing an execution means destroying
its key: the content can no longer be read and the chain still verifies.

What is encrypted: EVERYTHING in the payload of a covered event type
except the operational keys listed in PUBLIC_FIELDS. The allowlist
is fail-safe: a new payload field is encrypted by default, not exposed
by omission — adding a genuinely public field requires listing it here.
Everything else (event types, tools, verdicts, states, relationships,
timings and the hashes of what was observed) remains visible. See the
limits in the report: content-derived hashes, recovery reason notes,
message headers and shared knowledge in memory are NOT covered, and
none of this reaches external systems or backups that keep a key.

Covered event types are the keys of PUBLIC_FIELDS. Events WITHOUT an
execution_id (e.g. some MEMORY_UPDATED knowledge records) cannot be
encrypted under the per-execution scheme and stay plaintext — see the
README "Erasure limitations" section.

Disabled by default: enable by wrapping the store (or with
VEXINTER_KEYRING_DIR in the service).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .core.crypto import canonical_json

_log = logging.getLogger(__name__)
from .core.events import Event, EventDraft, EventType
from .core.store import EventQuery

_V = 1

# Payload keys allowed to remain PLAINTEXT for each covered event type.
# Every other payload key is encrypted (fail-safe): operational metadata
# stays auditable, content is protected by default.
PUBLIC_FIELDS: dict[str, tuple[str, ...]] = {
    EventType.TASK_CREATED.value: ("instance_id", "credential_id", "budget"),
    EventType.TOOL_INVOKED.value: ("tool", "idempotency_key", "client_key",
                                   "invoked_by"),
    EventType.TOOL_RETURNED.value: ("ok",),
    EventType.STATE_OBSERVED.value: ("tag", "system", "consistency"),
    EventType.VERIFIER_VERDICT.value: ("status", "provisional",
                                       "followup_required", "scope"),
    EventType.MEMORY_UPDATED.value: ("knowledge_id", "status", "new_status"),
    # E2E envelopes carry agent headers and the full signed envelope:
    # only the routing/ack fields stay plaintext.
    EventType.MESSAGE_SENT.value: ("message_id", "to", "sequence",
                                   "body_hash"),
    EventType.MESSAGE_REJECTED.value: ("message_id", "to", "reason"),
}


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode())


class InMemoryKeyRing:
    """In-memory key ring (tests and embedded use): keys are lost when
    the process ends."""

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}
        self._erased: set[str] = set()
        self._lock = threading.Lock()

    def get(self, execution_id: str) -> bytes | None:
        return self._keys.get(execution_id)

    def get_or_create(self, execution_id: str) -> bytes | None:
        """None if the execution was erased: it is not re-encrypted with
        another key."""
        with self._lock:
            if execution_id in self._erased:
                return None
            k = self._keys.get(execution_id)
            if k is None:
                k = self._keys[execution_id] = os.urandom(32)
            return k

    def destroy(self, execution_id: str) -> bool:
        with self._lock:
            existed = self._keys.pop(execution_id, None) is not None
            self._erased.add(execution_id)
            return existed

    def is_erased(self, execution_id: str) -> bool:
        return execution_id in self._erased


class FileKeyRing:
    """File key ring: one key per execution, OUTSIDE the store.

    The directory must be on a different volume from the database and
    must NOT be part of its backups: if a backup keeps both the store
    and the key, erasure is useless. Destroying a key overwrites the
    file and deletes it (best effort: a journaled file system or an SSD
    does not guarantee physical erasure — the real guarantee is
    cryptographic: once the key is gone the ciphertext is unrecoverable)
    and leaves a '.erased' marker that prevents creating another key
    for that execution.
    """

    def __init__(self, directory) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def _base(self, execution_id: str) -> Path:
        return self._dir / hashlib.sha256(
            execution_id.encode()).hexdigest()[:40]

    def _key_path(self, execution_id: str) -> Path:
        return self._base(execution_id).with_suffix(".key")

    def is_erased(self, execution_id: str) -> bool:
        return self._base(execution_id).with_suffix(".erased").exists()

    def get(self, execution_id: str) -> bytes | None:
        with self._lock:
            if execution_id in self._cache:
                return self._cache[execution_id]
            if self.is_erased(execution_id):
                return None
            p = self._key_path(execution_id)
            if p.exists():
                k = p.read_bytes()
                self._cache[execution_id] = k
                return k
            return None

    def get_or_create(self, execution_id: str) -> bytes | None:
        with self._lock:
            if self.is_erased(execution_id):
                return None
            if execution_id in self._cache:
                return self._cache[execution_id]
            p = self._key_path(execution_id)
            k = os.urandom(32)
            try:
                with open(p, "xb") as f:
                    f.write(k)
            except FileExistsError:
                k = p.read_bytes()
            self._cache[execution_id] = k
            return k

    def destroy(self, execution_id: str) -> bool:
        with self._lock:
            p = self._key_path(execution_id)
            existed = p.exists()
            if existed:
                try:
                    # O_WRONLY without O_CREAT: if the file vanished
                    # between exists() and open, we do NOT recreate it —
                    # a concurrent get_or_create() may have just written
                    # a fresh key that must not be zeroed.
                    fd = os.open(p, os.O_WRONLY)
                    try:
                        os.write(fd, b"\x00" * 32)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    p.unlink(missing_ok=True)
                try:  # best effort: persist the unlink itself
                    dfd = os.open(self._dir, os.O_RDONLY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError:
                    pass
            self._base(execution_id).with_suffix(".erased").write_text(
                datetime.now(UTC).isoformat())
            self._cache.pop(execution_id, None)
            return existed


_ERASED_SCAN_LIMIT = 100_000


def _aad(execution_id: str, event_type: str, fields) -> bytes:
    """The ciphertext is bound to its execution, its type and its fields:
    a block cannot be transplanted to another event."""
    return canonical_json({"v": _V, "execution_id": execution_id,
                           "type": event_type,
                           "fields": sorted(fields)}).encode()


class PrivacyStore:
    """Wrapper over any store with the same interface (append, get,
    query, verify_chain, take_checkpoint, head...). Everything that is
    not privacy-related is delegated to the inner store."""

    def __init__(self, inner, keyring) -> None:
        self._inner = inner
        self._ring = keyring
        self._erase_lock = threading.Lock()
        self._erased_ids: set[str] | None = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # -- write -------------------------------------------------------------
    def _encrypt(self, draft: EventDraft) -> EventDraft:
        public = PUBLIC_FIELDS.get(draft.type.value)
        if (not public or not isinstance(draft.payload, dict)
                or not draft.execution_id):
            return draft
        present = [c for c in draft.payload
                   if c not in public and c not in ("_enc", "_redacted")]
        if not present:
            return draft
        plain = {c: draft.payload[c] for c in present}
        rest = {k: v for k, v in draft.payload.items()
                if k not in present and k not in ("_enc", "_redacted")}
        # is_erased() also consults the persisted PRIVACY_ERASED events, so
        # an execution erased before a restart cannot get a fresh key even
        # when the keyring itself is volatile (InMemoryKeyRing).
        key = (None if self.is_erased(draft.execution_id)
               else self._ring.get_or_create(draft.execution_id))
        if key is None:             # erased execution: content is discarded
            rest["_redacted"] = sorted(present)
        else:
            nonce = os.urandom(12)
            ct = AESGCM(key).encrypt(
                nonce, canonical_json(plain).encode(),
                _aad(draft.execution_id, draft.type.value, present))
            rest["_enc"] = {"v": _V, "fields": sorted(present),
                            "n": _b64e(nonce), "ct": _b64e(ct)}
        return draft.model_copy(update={"payload": rest})

    def append(self, draft: EventDraft, stream: str) -> Event:
        return self._decrypt(self._inner.append(self._encrypt(draft), stream))

    # -- read --------------------------------------------------------------
    def _decrypt(self, ev: Event) -> Event:
        p = ev.payload
        if not isinstance(p, dict) or "_enc" not in p:
            return ev
        enc = p["_enc"] if isinstance(p["_enc"], dict) else {}
        fields = list(enc.get("fields") or enc.get("campos") or [])
        rest = {k: v for k, v in p.items()
                if k not in ("_enc", "_redactado", "_redacted")}
        key = self._ring.get(ev.execution_id) if ev.execution_id else None
        if key is None:
            rest["_redacted"] = fields
        else:
            try:
                plain = json.loads(AESGCM(key).decrypt(
                    _b64d(enc["n"]), _b64d(enc["ct"]),
                    _aad(ev.execution_id or "", ev.type.value, fields)))
                rest.update(plain)
            except (InvalidTag, ValueError, KeyError, TypeError):
                rest["_redacted"] = fields      # unreadable: do not crash
        return ev.model_copy(update={"payload": rest})

    def get(self, event_id: str):
        ev = self._inner.get(event_id)
        return self._decrypt(ev) if ev is not None else None

    def query(self, q=None):
        return [self._decrypt(e) for e in self._inner.query(q)]

    # -- erasure ------------------------------------------------------------
    def _known_erased(self) -> set[str]:
        """Executions erased according to the persisted PRIVACY_ERASED
        events. Rebuilds the set once: an InMemoryKeyRing loses its erased
        markers on restart while the event log survives (e.g. Postgres),
        and without this a destroyed key could silently come back."""
        if self._erased_ids is None:
            try:
                evs = self._inner.query(EventQuery(
                    types={EventType.PRIVACY_ERASED}, limit=_ERASED_SCAN_LIMIT))
                if len(evs) >= _ERASED_SCAN_LIMIT:
                    _log.warning(
                        "PRIVACY_ERASED scan hit the %d limit — recent "
                        "erasures may be missing from the erased set",
                        _ERASED_SCAN_LIMIT)
                self._erased_ids = {
                    e.payload["execution_id"] for e in evs
                    if isinstance(e.payload, dict)
                    and e.payload.get("execution_id")
                }
            except Exception:  # noqa: BLE001 - store without query support
                self._erased_ids = set()
        return self._erased_ids

    def is_erased(self, execution_id: str) -> bool:
        return (self._ring.is_erased(execution_id)
                or execution_id in self._known_erased())

    def erase(self, execution_id: str, reason: str = "",
              requested_by: str = "") -> bool:
        """Destroys the execution key. Returns True if there was a key.

        Serialized per store: concurrent erases cannot interleave the
        key destruction with the audit append."""
        with self._erase_lock:
            # Order is deliberate: destroy FIRST, audit SECOND — we must
            # never record "erased" while the key is still alive. If the
            # audit append fails below, the exception propagates and the
            # caller sees the failure; FileKeyRing's .erased marker still
            # protects the execution on restart.
            existed = self._ring.destroy(execution_id)
            try:
                self._inner.append(EventDraft(
                    type=EventType.PRIVACY_ERASED,
                    payload={"execution_id": execution_id, "reason": reason,
                             "requested_by": requested_by,
                             "had_key": existed}),
                    stream="privacy")
            except Exception:
                _log.critical(
                    "PRIVACY_ERASED audit append failed for %s: key is "
                    "destroyed but the erasure event was NOT logged",
                    execution_id)
                raise
            self._erased_ids = None      # refresh on next is_erased()
            return existed
