from __future__ import annotations

import base64
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from pydantic import BaseModel

from ..core.events import EventDraft, EventType, new_id, now
from ..core.store import InMemoryEventStore


class IdentityError(Exception): ...


class UnknownAgentError(IdentityError): ...


class RevokedError(IdentityError): ...


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode())


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Principal(BaseModel):
    """Identity presented in an operation (§9.2).

    `instance_id`/`credential_id` are optional ONLY in development mode;
    with fail-closed authorization, no credential means no operation.
    """

    agent_id: str
    instance_id: str | None = None
    credential_id: str | None = None


class AgentIdentity(BaseModel):
    """LOGICAL agent (§9.1): stable permissions, survives instances.

    Policy semantics: None = unrestricted (development ONLY, §14);
    set() = nothing allowed; set(...) = allowlist.
    """

    agent_id: str
    display_name: str = ""
    allowed_tools: set[str] | None = None
    allowed_recipients: set[str] | None = None


class AgentInstance(BaseModel):
    """Running instance (§9.1): revocable independently."""

    instance_id: str
    agent_id: str
    status: Literal["active", "revoked"] = "active"


class Credential(BaseModel):
    """Session credential (§9.1): public keys + validity window.

    PRIVATE keys live only in the agent; Vexinter never sees them.
    """

    credential_id: str
    agent_id: str
    instance_id: str
    signing_pub: str
    encryption_pub: str
    not_before: datetime
    expires_at: datetime
    status: CredentialStatus = CredentialStatus.ACTIVE
    revoked_reason: str | None = None
    superseded_by: str | None = None
    grace_until: datetime | None = None

    @property
    def valid_until(self) -> datetime:
        """Effective end of validity: the expiration or, if it was replaced
        by a rotation, the end of its grace period (whichever comes first)."""
        if self.grace_until is None:
            return self.expires_at
        return min(self.expires_at, self.grace_until)


class IdentityService:
    """§9.1–§9.2: lifecycle of identities, instances and credentials."""

    def __init__(
        self,
        store: InMemoryEventStore | None = None,
        credential_ttl_s: float = 3600.0,
        rotation_grace_s: float = 900.0,
    ):
        self._store = store
        self._ttl = credential_ttl_s
        self._grace = rotation_grace_s
        self._agents: dict[str, AgentIdentity] = {}
        self._instances: dict[str, AgentInstance] = {}
        self._credentials: dict[str, Credential] = {}

    def enroll_agent(
        self,
        agent_id: str,
        *,
        display_name: str = "",
        allowed_tools: set[str] | None = None,
        allowed_recipients: set[str] | None = None,
    ) -> AgentIdentity:
        if agent_id in self._agents:
            raise IdentityError(f"agent already registered: {agent_id}")
        a = AgentIdentity(
            agent_id=agent_id,
            display_name=display_name,
            allowed_tools=allowed_tools,
            allowed_recipients=allowed_recipients,
        )
        self._agents[agent_id] = a
        self._log(
            EventType.IDENTITY_ENROLLED,
            {
                "agent_id": agent_id,
                "display_name": display_name,
                "allowed_tools": (
                    sorted(allowed_tools) if allowed_tools is not None else None
                ),
                "allowed_recipients": (
                    sorted(allowed_recipients)
                    if allowed_recipients is not None
                    else None
                ),
            },
        )
        return a

    def spawn_instance(self, agent_id: str) -> AgentInstance:
        if agent_id not in self._agents:
            raise UnknownAgentError(agent_id)
        inst = AgentInstance(instance_id=new_id("inst"), agent_id=agent_id)
        self._instances[inst.instance_id] = inst
        self._log(
            EventType.INSTANCE_SPAWNED,
            {"instance_id": inst.instance_id, "agent_id": agent_id},
        )
        return inst

    def issue_credential(
        self, instance_id: str, signing_pub: str, encryption_pub: str
    ) -> Credential:
        """Registers a credential from the agent's PUBLIC keys."""
        inst = self._instances.get(instance_id)
        if inst is None or inst.status == "revoked":
            raise RevokedError(f"nonexistent or revoked instance: {instance_id}")
        self._check_public_keys(signing_pub, encryption_pub)
        t0 = now()
        cred = Credential(
            credential_id=new_id("cred"),
            agent_id=inst.agent_id,
            instance_id=instance_id,
            signing_pub=signing_pub,
            encryption_pub=encryption_pub,
            not_before=t0,
            expires_at=t0 + timedelta(seconds=self._ttl),
        )
        self._credentials[cred.credential_id] = cred
        self._log(
            EventType.CREDENTIAL_ISSUED,
            {
                "credential_id": cred.credential_id,
                "agent_id": cred.agent_id,
                "instance_id": instance_id,
                "signing_pub": signing_pub,
                "encryption_pub": encryption_pub,
                "not_before": cred.not_before.isoformat(),
                "expires_at": cred.expires_at.isoformat(),
            },
        )
        return cred

    @staticmethod
    def _check_public_keys(signing_pub: str, encryption_pub: str) -> None:
        """A malformed public key cannot enter the registry: it would make
        that credential's authentication fail on every request."""
        try:
            Ed25519PublicKey.from_public_bytes(b64d(signing_pub))
            X25519PublicKey.from_public_bytes(b64d(encryption_pub))
        except Exception as e:
            raise IdentityError(f"invalid public key: {e}") from e

    def rotate_credential(
        self, credential_id: str, signing_pub: str, encryption_pub: str
    ) -> Credential:
        """Key rotation with a grace period (§9.3).

        Issues a NEW credential for the same instance, with new keys, and
        keeps the previous one valid for `rotation_grace_s` more seconds
        (without exceeding its own expiration), so that requests and
        messages in flight signed with it are not lost. After the grace
        period it stops being valid. An already replaced credential cannot
        be rotated again.
        """
        old = self.assert_credential_valid(credential_id)
        if old.superseded_by is not None:
            raise IdentityError(
                f"the credential was already replaced by {old.superseded_by}"
            )
        if signing_pub == old.signing_pub or encryption_pub == old.encryption_pub:
            raise IdentityError("rotation requires new keys")
        self._check_public_keys(signing_pub, encryption_pub)
        t0 = now()
        new = Credential(
            credential_id=new_id("cred"),
            agent_id=old.agent_id,
            instance_id=old.instance_id,
            signing_pub=signing_pub,
            encryption_pub=encryption_pub,
            not_before=t0,
            expires_at=t0 + timedelta(seconds=self._ttl),
        )
        # Insert the successor FIRST: encryption_pub_for_agent prefers
        # non-superseded credentials with the latest not_before, so the
        # new key wins the moment it exists — there is no window where
        # only the old (about-to-be-superseded) key is visible.
        self._credentials[new.credential_id] = new
        old.superseded_by = new.credential_id
        old.grace_until = min(t0 + timedelta(seconds=self._grace), old.expires_at)
        self._log(
            EventType.CREDENTIAL_ISSUED,
            {
                "credential_id": new.credential_id,
                "agent_id": new.agent_id,
                "instance_id": new.instance_id,
                "signing_pub": signing_pub,
                "encryption_pub": encryption_pub,
                "not_before": new.not_before.isoformat(),
                "expires_at": new.expires_at.isoformat(),
                "rotates": old.credential_id,
                "grace_until": old.grace_until.isoformat(),
            },
        )
        return new

    def identity(self, agent_id: str) -> AgentIdentity:
        a = self._agents.get(agent_id)
        if a is None:
            raise UnknownAgentError(agent_id)
        return a

    def get_credential(self, credential_id: str) -> Credential:
        """Credential as registered (PUBLIC keys), without judging its
        validity: used to verify after the fact a signature made while the
        credential was valid (§9.5)."""
        c = self._credentials.get(credential_id)
        if c is None:
            raise UnknownAgentError(f"unknown credential: {credential_id}")
        return c

    def assert_credential_valid(self, credential_id: str) -> Credential:
        """Authentication (§9.2): is whoever it claims to be valid NOW?"""
        c = self._credentials.get(credential_id)
        if c is None:
            raise UnknownAgentError(f"unknown credential: {credential_id}")
        t = now()
        if c.status == CredentialStatus.REVOKED:
            raise RevokedError(f"revoked credential: {credential_id}")
        inst = self._instances.get(c.instance_id)
        if inst is None or inst.status == "revoked":
            raise RevokedError(f"revoked instance of {c.agent_id}: {c.instance_id}")
        if t < c.not_before or t > c.valid_until:
            raise IdentityError("credential outside its validity window")
        return c

    def encryption_pub_for_agent(self, agent_id: str) -> str:
        """Recipient's most recent active encryption key.

        Design note (rotation, §9.3): in production, rotation must keep a
        grace period for messages in flight addressed to previous
        credentials. Local delivery is immediate, so v1 does not need it.
        """
        cands = [
            c
            for c in self._credentials.values()
            if c.agent_id == agent_id
            and c.status == CredentialStatus.ACTIVE
            and c.valid_until > now()
        ]
        if not cands:
            raise UnknownAgentError(f"no active credential for: {agent_id}")
        return max(
            cands, key=lambda c: (c.superseded_by is None, c.not_before)
        ).encryption_pub

    def revoke_credential(self, credential_id: str, reason: str) -> None:
        c = self._credentials.get(credential_id)
        if c is None:
            raise UnknownAgentError(credential_id)
        self._apply_credential_revocation(c, reason)

        self._log(
            EventType.CREDENTIAL_REVOKED,
            {
                "credential_id": c.credential_id,
                "agent_id": c.agent_id,
                "reason": reason,
            },
        )

    def _apply_credential_revocation(self, c: Credential, reason: str) -> None:
        c.status = CredentialStatus.REVOKED
        c.revoked_reason = reason

    def revoke_instance(self, instance_id: str, reason: str) -> None:
        inst = self._instances.get(instance_id)
        if inst is None:
            raise UnknownAgentError(instance_id)
        self._apply_instance_revocation(inst, reason)
        self._log(
            EventType.IDENTITY_REVOKED,
            {
                "instance_id": instance_id,
                "agent_id": inst.agent_id,
                "scope": "instance",
                "reason": reason,
            },
        )

    def _apply_instance_revocation(self, inst: AgentInstance, reason: str) -> None:
        inst.status = "revoked"
        for c in self._credentials.values():
            if (
                c.instance_id == inst.instance_id
                and c.status == CredentialStatus.ACTIVE
            ):
                self._apply_credential_revocation(c, reason)

    def prune_credentials(self) -> int:
        """Drops credentials that can never authenticate again:
        revoked, or past their effective validity (expires_at or the
        end of a rotation grace period). The event log keeps the full
        history; this only bounds the in-memory lookup table."""
        t = now()
        dead = [cid for cid, c in self._credentials.items()
                if c.status == CredentialStatus.REVOKED
                or t > c.valid_until]
        for cid in dead:
            del self._credentials[cid]
        return len(dead)

    def list_agents(self) -> list[AgentIdentity]:
        return list(self._agents.values())

    @classmethod
    def rebuild(
        cls, store, credential_ttl_s: float = 3600.0, rotation_grace_s: float = 900.0
    ) -> IdentityService:
        from ..core.store import EventQuery

        svc = cls(store, credential_ttl_s, rotation_grace_s)
        types = {
            EventType.IDENTITY_ENROLLED,
            EventType.INSTANCE_SPAWNED,
            EventType.CREDENTIAL_ISSUED,
            EventType.IDENTITY_REVOKED,
            EventType.CREDENTIAL_REVOKED,
        }
        for e in store.query(EventQuery(types=types, limit=1_000_000)):
            p = e.payload or {}
            if e.type == EventType.IDENTITY_ENROLLED:
                svc._agents[p["agent_id"]] = AgentIdentity(
                    agent_id=p["agent_id"],
                    display_name=p.get("display_name", ""),
                    allowed_tools=(
                        set(p["allowed_tools"])
                        if p.get("allowed_tools") is not None
                        else None
                    ),
                    allowed_recipients=(
                        set(p["allowed_recipients"])
                        if p.get("allowed_recipients") is not None
                        else None
                    ),
                )
            elif e.type == EventType.INSTANCE_SPAWNED:
                svc._instances[p["instance_id"]] = AgentInstance(
                    instance_id=p["instance_id"], agent_id=p["agent_id"]
                )
            elif e.type == EventType.CREDENTIAL_ISSUED:
                svc._instances.setdefault(
                    p["instance_id"],
                    AgentInstance(instance_id=p["instance_id"], agent_id=p["agent_id"]),
                )
                svc._credentials[p["credential_id"]] = Credential(
                    credential_id=p["credential_id"],
                    agent_id=p["agent_id"],
                    instance_id=p["instance_id"],
                    signing_pub=p["signing_pub"],
                    encryption_pub=p["encryption_pub"],
                    not_before=datetime.fromisoformat(p["not_before"]),
                    expires_at=datetime.fromisoformat(p["expires_at"]),
                )
                anterior = svc._credentials.get(p.get("rotates") or "")
                if anterior is not None:
                    anterior.superseded_by = p["credential_id"]
                    if p.get("grace_until"):
                        anterior.grace_until = datetime.fromisoformat(p["grace_until"])
            elif e.type == EventType.IDENTITY_REVOKED:
                if (
                    p.get("scope") == "instance"
                    and p.get("instance_id") in svc._instances
                ):
                    svc._apply_instance_revocation(
                        svc._instances[p["instance_id"]], p.get("reason", "")
                    )
                elif p.get("credential_id") in svc._credentials:
                    svc._apply_credential_revocation(
                        svc._credentials[p["credential_id"]], p.get("reason", "")
                    )
            elif (
                e.type == EventType.CREDENTIAL_REVOKED
                and p.get("credential_id") in svc._credentials
            ):
                svc._apply_credential_revocation(
                    svc._credentials[p["credential_id"]], p.get("reason", "")
                )
        svc.prune_credentials()
        return svc

    def can_invoke_tool(self, credential_id: str, tool_key: str) -> bool:
        try:
            c = self.assert_credential_valid(credential_id)
        except IdentityError:
            return False
        a = self._agents[c.agent_id]
        if a.allowed_tools is None:
            return True
        return tool_key in a.allowed_tools

    def can_communicate_with(self, credential_id: str, to_agent: str) -> bool:
        try:
            c = self.assert_credential_valid(credential_id)
        except IdentityError:
            return False
        a = self._agents[c.agent_id]
        if a.allowed_recipients is None:
            return True
        return to_agent in a.allowed_recipients

    def _log(self, etype: EventType, payload: dict) -> None:
        if self._store is not None:
            self._store.append(
                EventDraft(type=etype, payload=payload), stream="identity"
            )

    def list_credentials(self, agent_id: str | None = None) -> list[Credential]:
        """Credentials with PUBLIC keys (§13.4): there are never private
        ones here."""
        return [
            c
            for c in self._credentials.values()
            if agent_id is None or c.agent_id == agent_id
        ]

    def list_instances(self, agent_id: str | None = None) -> list[AgentInstance]:
        return [
            i
            for i in self._instances.values()
            if agent_id is None or i.agent_id == agent_id
        ]
