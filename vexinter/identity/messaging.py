from __future__ import annotations

import threading
from enum import Enum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..core.events import Event, EventDraft, EventType, now
from ..core.store import EventQuery
from .envelope import Envelope, sha256_b, signable_material
from .service import IdentityError, b64d

# Bounds for in-memory caches: the log is authoritative, these only
# keep hot state small. Older envelopes remain queryable via events.
_MAX_INBOX_PER_AGENT = 1_000
_MAX_SENT_CACHE = 10_000

class RejectionReason(str, Enum):
    INVALID_CREDENTIAL = "invalid_credential"     
    BINDING_MISMATCH   = "binding_mismatch"   
    BAD_SIGNATURE      = "bad_signature"
    TAMPERING          = "tampering"
    STALE_TS           = "stale_timestamp"
    REPLAY             = "replay"
    NOT_AUTHORIZED     = "not_authorized"


class Rejection(Exception):
    def __init__(self, reason: RejectionReason, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason.value}: {detail}")


def verify_envelope(env: Envelope, cred) -> None:
    """Envelope verification by the RECIPIENT (§9.5).

    Anyone who knows the recipient's public key can encrypt a message for
    them: what authenticates the sender is the Ed25519 signature. The
    recipient checks it with the credential's registered PUBLIC keys,
    without relying solely on the service having done so. It requires the
    signature to have been made within the credential's validity window;
    it does not judge a later revocation.
    """
    if (cred.credential_id != env.header.from_credential
            or cred.agent_id != env.header.from_agent
            or cred.instance_id != env.header.from_instance):
        raise Rejection(RejectionReason.BINDING_MISMATCH,
                        "the credential does not belong to the declared sender")
    try:
        Ed25519PublicKey.from_public_bytes(b64d(cred.signing_pub)).verify(
            b64d(env.signature), signable_material(env))
    except (InvalidSignature, ValueError):
        raise Rejection(RejectionReason.BAD_SIGNATURE)
    if sha256_b(b64d(env.ct)) != env.ct_hash:
        raise Rejection(RejectionReason.TAMPERING, "ct_hash does not match")
    if not (cred.not_before <= env.header.ts <= cred.valid_until):
        raise Rejection(RejectionReason.STALE_TS,
                        "signed outside the credential's validity window")


class MessageService:
    """§9.2 + §9.4 + §9.5: authenticated, intact and confidential channel.

    The service checks FIVE things, in this order (authentication before
    authorization — both are required, §9.2):
      1. the sender's credential is valid NOW (not revoked, active
         instance, within its window);
      2. the credential belongs to the agent/instance declared in the
         envelope;
      3. the Ed25519 signature is valid over all signable material;
      4. the timestamp is within the clock window and the sequence is
         strictly increasing (anti-replay, §9.4);
      5. the sender is authorized to communicate with the recipient.
    """

    def __init__(self, identity_service, store,
                 clock=None, max_skew_s: float = 300.0):
        self._ids = identity_service
        self._store = store
        self._clock = clock or (lambda: now())
        self._max_skew_s = max_skew_s
        self._last_seq: dict[tuple[str, str], int] = {}
        self._inboxes: dict[str, list[Envelope]] = {}
        self._sent: dict[str, Event] = {}
        self._received_ids: set[str] = set()
        # Inboxes hydrate lazily per agent and stay bounded: the event log
        # is the source of truth, memory is only a cache.
        self._inbox_loaded: set[str] = set()
        self._hydrate_lock = threading.RLock()
        # Anti-replay state must exist before the service is usable:
        # rebuild it eagerly so a caller can never send/receive against
        # an empty replay cache.
        self.restore_from_log()

    def send(self, env: Envelope) -> Envelope:
        try:
            self._verify(env)
        except Rejection as r:
            self._reject(env, r)
            raise
        ev = self._store.append(EventDraft(
            type=EventType.MESSAGE_SENT, agent_id=env.header.from_agent,
            task_id=env.header.task_id, execution_id=env.header.execution_id,
            payload={"message_id": env.header.message_id,
                     "to": env.header.to_agent,
                     "sequence": env.header.sequence,
                     "body_hash": env.body_hash,
                     "envelope": env.model_dump(mode="json")}),
            stream=self._stream_for(env.header.task_id))
        self._sent[env.header.message_id] = ev
        inbox = self._inboxes.setdefault(env.header.to_agent, [])
        inbox.append(env)
        del inbox[:-_MAX_INBOX_PER_AGENT]
        # A live send marks the inbox hydrated: pending_for must not
        # re-scan the log and duplicate it.
        self._inbox_loaded.add(env.header.to_agent)
        if len(self._sent) > _MAX_SENT_CACHE:
            for k in list(self._sent)[:len(self._sent) - _MAX_SENT_CACHE]:
                del self._sent[k]
        return env

    def receive(self, recipient_keys, env: Envelope) -> bytes:
        """Recipient side: verifies the sender's signature ON ITS OWN,
        decrypts E2E with ITS key and verifies body_hash (§9.5)."""
        try:
            cred = self._ids.get_credential(env.header.from_credential)
        except IdentityError as e:
            raise Rejection(RejectionReason.INVALID_CREDENTIAL, str(e))
        verify_envelope(env, cred)
        if env.header.message_id in self._received_ids:
            raise Rejection(RejectionReason.REPLAY,
                            f"message already received: "
                            f"{env.header.message_id}")
        pt = recipient_keys.decrypt_body(env)
        if sha256_b(pt) != env.body_hash:
            raise Rejection(RejectionReason.TAMPERING, "body_hash does not match")
        sent = self._find_sent(env.header.message_id)
        self._store.append(EventDraft(
            type=EventType.MESSAGE_RECEIVED, agent_id=env.header.to_agent,
            task_id=env.header.task_id, execution_id=env.header.execution_id,
            caused_by=sent.id if sent else None,
            payload={"message_id": env.header.message_id,
                     "from": env.header.from_agent, "verified": True}),
            stream=self._stream_for(env.header.task_id))
        self._received_ids.add(env.header.message_id)
        return pt

    def next_sequence(self, from_agent: str, to_agent: str) -> int:
        return self._last_seq.get((from_agent, to_agent), 0) + 1

    def pending_for(self, agent_id: str) -> list[Envelope]:
        self._hydrate_inbox(agent_id)
        return list(self._inboxes.get(agent_id, []))

    def _hydrate_inbox(self, agent_id: str) -> None:
        """Loads this agent's pending envelopes from the log once.

        restore_from_log() deliberately skips inbox hydration (it would
        materialize every ciphertext in memory); the first pending_for()
        per agent pays one scan instead."""
        with self._hydrate_lock:
            if agent_id in self._inbox_loaded:
                return
            self._inbox_loaded.add(agent_id)
            self._hydrate_inbox_locked(agent_id)

    def _hydrate_inbox_locked(self, agent_id: str) -> None:
        from ..core.store import EventQuery
        inbox = self._inboxes.setdefault(agent_id, [])
        have = {e.header.message_id for e in inbox}
        for e in self._store.query(
                EventQuery(types={EventType.MESSAGE_SENT},
                           payload_eq={"to": agent_id},
                           limit=1_000_000)):
            p = e.payload or {}
            if (p.get("to") == agent_id and p.get("envelope")
                    and p.get("message_id") not in self._received_ids
                    and p["message_id"] not in have):
                inbox.append(Envelope(**p["envelope"]))
        del inbox[:-_MAX_INBOX_PER_AGENT]

    def _verify(self, env: Envelope):
        try:
            cred = self._ids.assert_credential_valid(env.header.from_credential)
        except IdentityError as e:
            raise Rejection(RejectionReason.INVALID_CREDENTIAL, str(e))
        if (cred.agent_id != env.header.from_agent
                or cred.instance_id != env.header.from_instance):
            raise Rejection(RejectionReason.BINDING_MISMATCH,
                            "the credential does not belong to the declared sender")
        pub = Ed25519PublicKey.from_public_bytes(b64d(cred.signing_pub))
        try:
            pub.verify(b64d(env.signature), signable_material(env))
        except InvalidSignature:
            raise Rejection(RejectionReason.BAD_SIGNATURE)
        if sha256_b(b64d(env.ct)) != env.ct_hash:
            raise Rejection(RejectionReason.TAMPERING, "ct_hash does not match")
        skew = abs((self._clock() - env.header.ts).total_seconds())
        if skew > self._max_skew_s:
            raise Rejection(RejectionReason.STALE_TS,
                            f"skew of {skew:.0f}s")
        pair = (env.header.from_agent, env.header.to_agent)
        if env.header.sequence <= self._last_seq.get(pair, 0):
            raise Rejection(RejectionReason.REPLAY,
                            f"sequence {env.header.sequence} already seen")
        if not self._ids.can_communicate_with(env.header.from_credential,
                                              env.header.to_agent):
            raise Rejection(RejectionReason.NOT_AUTHORIZED,
                            f"{env.header.from_agent} cannot write to "
                            f"{env.header.to_agent}")
        self._last_seq[pair] = env.header.sequence

    def _reject(self, env: Envelope, r: Rejection) -> None:
        """§14: the failed attempt is audited — WITHOUT message content.

        Routing metadata (sender, task, execution) IS recorded: an audit
        event that cannot say who tried to send is useless."""
        self._store.append(EventDraft(
            type=EventType.MESSAGE_REJECTED, agent_id=env.header.from_agent,
            task_id=env.header.task_id, execution_id=env.header.execution_id,
            payload={"message_id": env.header.message_id,
                     "to": env.header.to_agent,
                     "reason": r.reason.value, "detail": r.detail}),
            stream=self._stream_for(env.header.task_id))

    def _find_sent(self, message_id: str) -> Event | None:
        ev = self._sent.get(message_id)
        if ev is not None:
            return ev
        for e in reversed(self._store.query(
                EventQuery(types={EventType.MESSAGE_SENT}, limit=1_000_000))):
            if e.payload and e.payload.get("message_id") == message_id:
                self._sent[message_id] = e
                if len(self._sent) > _MAX_SENT_CACHE:
                    for k in list(self._sent)[:len(self._sent) - _MAX_SENT_CACHE]:
                        del self._sent[k]
                return e
        return None

    def restore_from_log(self) -> None:
        """§15: rebuilds anti-replay sequences and inboxes from the log."""
        from ..core.store import EventQuery
        received = set()
        for e in self._store.query(
                EventQuery(types={EventType.MESSAGE_RECEIVED}, limit=1_000_000)):
            if e.payload:
                mid = e.payload.get("message_id")
                if mid:
                    received.add(mid)
        self._received_ids |= received
        for e in self._store.query(
                EventQuery(types={EventType.MESSAGE_SENT}, limit=1_000_000)):
            p = e.payload or {}
            pair = (e.agent_id or "", str(p.get("to") or ""))
            if p.get("sequence"):
                self._last_seq[pair] = max(self._last_seq.get(pair, 0),
                                           p["sequence"])
            # Inbox envelopes are NOT restored here: pending_for()
            # hydrates them lazily per agent to bound memory.

    def acknowledge(self, message_id: str, body_hash: str,
                   claimed_recipient: str | None = None) -> Event | None:
        """Confirms E2E receipt: only someone who decrypted knows body_hash.

        The confirmation with the content hash is the implicit proof that
        the recipient could decrypt (§9.5); the server never saw the
        plaintext.
        """
        sent = self._find_sent(message_id)
        sent_payload: dict[str, Any] = (sent.payload or {}) if sent else {}
        if (sent is None or sent_payload.get("body_hash") != body_hash
                or sent_payload.get("to") != (claimed_recipient
                                              or sent_payload.get("to"))):
            return None
        recipient = sent_payload["to"]
        ev = self._store.append(EventDraft(
            type=EventType.MESSAGE_RECEIVED, agent_id=recipient,
            task_id=sent.task_id, execution_id=sent.execution_id,
            caused_by=sent.id,                      
            payload={"message_id": message_id, "from": sent.agent_id,
                     "verified": True}), stream=sent.stream)
        self._inboxes[recipient] = [
            e for e in self._inboxes.get(recipient, [])
            if e.header.message_id != message_id]
        self._received_ids.add(message_id)
        return ev

    @staticmethod
    def _stream_for(task_id: str | None) -> str:
        return f"task:{task_id}" if task_id else "comms"