"""Native multi-tenancy with per-organization RBAC (Item 11).

Provides data isolation, quotas and per-tenant billing.
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .events import EventDraft, EventType, new_id

_log = logging.getLogger(__name__)
from .store import InMemoryEventStore as EventStore


class TenantTier(str, Enum):
    """Tenant subscription/plan tiers."""
    FREE = "free"
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class TenantStatus(str, Enum):
    """Tenant statuses."""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    TRIAL = "trial"


class TenantRole(str, Enum):
    """RBAC roles within a tenant."""
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    DEVELOPER = "developer"


# Permissions by role (minimum subset)
ROLE_PERMISSIONS: dict[TenantRole, set[str]] = {
    TenantRole.OWNER: {"*"},  # All permissions
    TenantRole.ADMIN: {
        "tenant:read", "tenant:write", "tenant:billing",
        "agents:*", "tasks:*", "cases:*", "metrics:*",
        "members:read", "members:write", "members:invite", "members:remove"
    },
    TenantRole.OPERATOR: {
        "tenant:read", "agents:read", "agents:write",
        "tasks:read", "tasks:write", "cases:read", "cases:decide",
        "metrics:read"
    },
    TenantRole.DEVELOPER: {
        "tenant:read", "agents:*", "tasks:*", "metrics:read"
    },
    TenantRole.VIEWER: {
        "tenant:read", "agents:read", "tasks:read", "cases:read", "metrics:read"
    },
}


# Quotas by tier
TIER_QUOTAS: dict[TenantTier, dict[str, int]] = {
    TenantTier.FREE: {
        "max_agents": 5,
        "max_concurrent_tasks": 2,
        "max_executions_per_day": 100,
        "max_storage_mb": 100,
        "max_members": 3,
        "api_rate_limit_rps": 10,
    },
    TenantTier.STARTER: {
        "max_agents": 25,
        "max_concurrent_tasks": 10,
        "max_executions_per_day": 10_000,
        "max_storage_mb": 1_000,
        "max_members": 10,
        "api_rate_limit_rps": 50,
    },
    TenantTier.PROFESSIONAL: {
        "max_agents": 100,
        "max_concurrent_tasks": 50,
        "max_executions_per_day": 100_000,
        "max_storage_mb": 10_000,
        "max_members": 50,
        "api_rate_limit_rps": 200,
    },
    TenantTier.ENTERPRISE: {
        "max_agents": -1,  # No limit
        "max_concurrent_tasks": -1,
        "max_executions_per_day": -1,
        "max_storage_mb": -1,
        "max_members": -1,
        "api_rate_limit_rps": 1000,
    },
}


@dataclass
class Tenant:
    """Organization/Tenant with full isolation."""
    tenant_id: str
    name: str
    tier: TenantTier = TenantTier.FREE
    status: TenantStatus = TenantStatus.TRIAL
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def quotas(self) -> dict[str, int]:
        return TIER_QUOTAS.get(self.tier, TIER_QUOTAS[TenantTier.FREE])

    @property
    def is_active(self) -> bool:
        if self.status != TenantStatus.ACTIVE:
            return False
        return not (self.expires_at and datetime.now(UTC) > self.expires_at)


class TenantMember(BaseModel):
    """Member of a tenant with an RBAC role."""
    member_id: str
    tenant_id: str
    user_id: str  # External ID (email, sub, etc.)
    roles: set[TenantRole] = Field(default_factory=set)
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    invited_by: str | None = None
    is_active: bool = True

    def has_permission(self, permission: str) -> bool:
        """Checks whether the member has a specific permission."""
        for role in self.roles:
            perms = ROLE_PERMISSIONS.get(role, set())
            if "*" in perms:
                return True
            if permission in perms:
                return True
            # Wildcard check (e.g. "agents:*" covers "agents:read")
            for p in perms:
                if p.endswith("*") and permission.startswith(p[:-1]):
                    return True
        return False


class TenantService:
    """Multi-tenancy service: tenant, member, quota and isolation management."""

    def __init__(self, store: EventStore | None = None):
        self._store = store
        self._tenants: dict[str, Tenant] = {}
        self._members: dict[str, list[TenantMember]] = {}  # tenant_id -> members
        self._user_to_tenant: dict[str, str] = {}  # user_id -> tenant_id (for fast lookup)
        self._agent_to_tenant: dict[str, str] = {}  # agent_id -> tenant_id
        self._executions_today: dict[str, tuple[str, int]] = {}  # tenant_id -> (date, count)
        # PostgresEventStore exposes _pool; when present the tenant state is
        # persisted (survives restarts) and the dicts above act as a cache.
        self._pg = getattr(store, "_pool", None)
        if self._pg is not None:
            self._load_pg()

    def create_tenant(
        self,
        name: str,
        tier: TenantTier = TenantTier.FREE,
        owner_user_id: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Tenant:
        """Creates a new tenant with an optional owner."""
        tenant_id = new_id("tnt")
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            tier=tier,
            status=TenantStatus.ACTIVE if tier != TenantTier.FREE else TenantStatus.TRIAL,
            settings=settings or {},
        )
        self._tenants[tenant_id] = tenant
        self._save_tenant(tenant)

        # Create owner if provided
        if owner_user_id:
            self.add_member(tenant_id, owner_user_id, {TenantRole.OWNER})

        self._log_tenant_event("tenant_created", tenant_id, {"name": name, "tier": tier.value})
        return tenant

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def get_tenant_by_user(self, user_id: str) -> Tenant | None:
        tenant_id = self._user_to_tenant.get(user_id)
        if tenant_id:
            return self._tenants.get(tenant_id)
        return None

    def update_tenant(self, tenant_id: str, **kwargs) -> Tenant | None:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return None
        for key, value in kwargs.items():
            if hasattr(tenant, key):
                setattr(tenant, key, value)
        tenant.updated_at = datetime.now(UTC)
        self._log_tenant_event("tenant_updated", tenant_id, kwargs)
        self._save_tenant(tenant)
        return tenant

    def set_tenant_tier(self, tenant_id: str, tier: TenantTier) -> Tenant | None:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return None
        old_tier = tenant.tier
        tenant.tier = tier
        tenant.updated_at = datetime.now(UTC)
        self._log_tenant_event("tier_changed", tenant_id, {"from": old_tier.value, "to": tier.value})
        self._save_tenant(tenant)
        return tenant

    def suspend_tenant(self, tenant_id: str, reason: str = "") -> bool:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        tenant.status = TenantStatus.SUSPENDED
        tenant.updated_at = datetime.now(UTC)
        self._log_tenant_event("tenant_suspended", tenant_id, {"reason": reason})
        self._save_tenant(tenant)
        return True

    def activate_tenant(self, tenant_id: str) -> bool:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        tenant.status = TenantStatus.ACTIVE
        tenant.updated_at = datetime.now(UTC)
        self._log_tenant_event("tenant_activated", tenant_id, {})
        self._save_tenant(tenant)
        return True

    # === Member management (RBAC) ===

    def add_member(
        self,
        tenant_id: str,
        user_id: str,
        roles: set[TenantRole],
        invited_by: str | None = None,
    ) -> TenantMember | None:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return None

        # Check member quota
        current_members = len(self._members.get(tenant_id, []))
        if current_members >= tenant.quotas["max_members"] and tenant.quotas["max_members"] > 0:
            raise ValueError(f"Member quota exceeded for tier {tenant.tier.value}")

        member_id = new_id("mem")
        member = TenantMember(
            member_id=member_id,
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            invited_by=invited_by,
        )

        if tenant_id not in self._members:
            self._members[tenant_id] = []
        self._members[tenant_id].append(member)
        # First tenant wins: silently re-pointing the user would grant the
        # newest tenant access to resources meant for the first one.
        previo = self._user_to_tenant.setdefault(user_id, tenant_id)
        if previo != tenant_id:
            _log.warning(
                "user %s invited to tenant %s but already mapped to %s: "
                "keeping first mapping (member record still added)",
                user_id, tenant_id, previo)

        self._log_tenant_event("member_added", tenant_id, {
            "member_id": member_id, "user_id": user_id,
            "roles": [r.value for r in roles]
        })
        self._save_member(member)
        return member

    def remove_member(self, tenant_id: str, user_id: str) -> bool:
        members = self._members.get(tenant_id, [])
        for i, m in enumerate(members):
            if m.user_id == user_id:
                members.pop(i)
                self._user_to_tenant.pop(user_id, None)
                self._log_tenant_event("member_removed", tenant_id, {"user_id": user_id})
                self._delete_member(tenant_id, user_id)
                return True
        return False

    def get_member(self, tenant_id: str, user_id: str) -> TenantMember | None:
        for m in self._members.get(tenant_id, []):
            if m.user_id == user_id:
                return m
        return None

    def list_members(self, tenant_id: str) -> list[TenantMember]:
        return self._members.get(tenant_id, [])

    def update_member_roles(self, tenant_id: str, user_id: str, roles: set[TenantRole]) -> TenantMember | None:
        member = self.get_member(tenant_id, user_id)
        if not member:
            return None
        old_roles = member.roles
        member.roles = roles
        self._log_tenant_event("member_roles_updated", tenant_id, {
            "user_id": user_id, "from": [r.value for r in old_roles], "to": [r.value for r in roles]
        })
        self._save_member(member)
        return member

    # === Agent binding ===

    def assign_agent(self, tenant_id: str, agent_id: str) -> bool:
        """Binds an agent to a tenant. The agent inherits the tenant's
        active/suspended status and quotas on every credentialed request."""
        if tenant_id not in self._tenants:
            return False
        prev = self._agent_to_tenant.get(agent_id)
        if prev == tenant_id:
            return True
        tenant = self._tenants[tenant_id]
        if prev is None:
            used = sum(1 for t in self._agent_to_tenant.values() if t == tenant_id)
            limit = tenant.quotas.get("max_agents", -1)
            if limit >= 0 and used >= limit:
                raise ValueError(f"Agent quota exceeded for tier {tenant.tier.value}")
        self._agent_to_tenant[agent_id] = tenant_id
        self._log_tenant_event("agent_assigned", tenant_id,
                               {"agent_id": agent_id, "previous_tenant": prev})
        self._save_agent(agent_id, tenant_id)
        return True

    def unassign_agent(self, tenant_id: str, agent_id: str) -> bool:
        if self._agent_to_tenant.get(agent_id) != tenant_id:
            return False
        del self._agent_to_tenant[agent_id]
        self._log_tenant_event("agent_unassigned", tenant_id, {"agent_id": agent_id})
        self._delete_agent(agent_id)
        return True

    def tenant_of_agent(self, agent_id: str) -> Tenant | None:
        tenant_id = self._agent_to_tenant.get(agent_id)
        return self._tenants.get(tenant_id) if tenant_id else None

    def agents_of_tenant(self, tenant_id: str) -> list[str]:
        return [a for a, t in self._agent_to_tenant.items() if t == tenant_id]

    # === Usage quotas ===

    def executions_today(self, tenant_id: str) -> int:
        today = datetime.now(UTC).date().isoformat()
        if self._pg is not None:
            with self._pg.connection() as conn:
                row = conn.execute(
                    "SELECT count FROM vex_tenant_usage "
                    "WHERE tenant_id = %s AND day = %s",
                    (tenant_id, today)).fetchone()
            return int(row[0]) if row else 0
        day, count = self._executions_today.get(tenant_id, ("", 0))
        return count if day == today else 0

    def record_execution(self, tenant_id: str) -> None:
        today = datetime.now(UTC).date()
        if self._pg is not None:
            # Atomic upsert: safe under concurrent requests and replicas.
            with self._pg.connection() as conn:
                conn.execute(
                    "INSERT INTO vex_tenant_usage (tenant_id, day, count) "
                    "VALUES (%s, %s, 1) "
                    "ON CONFLICT (tenant_id, day) DO UPDATE "
                    "SET count = vex_tenant_usage.count + 1",
                    (tenant_id, today))
            return
        day_iso = today.isoformat()
        day, count = self._executions_today.get(tenant_id, ("", 0))
        self._executions_today[tenant_id] = (day_iso, count + 1 if day == day_iso else 1)

    def check_execution_quota(self, tenant_id: str) -> bool:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        limit = tenant.quotas.get("max_executions_per_day", -1)
        if limit < 0:
            return True
        return self.executions_today(tenant_id) < limit

    _quota_lock = __import__("threading").Lock()

    def try_record_execution(self, tenant_id: str) -> bool:
        """Atomically checks the daily quota and records the execution.

        Returns False when the quota is already exhausted. The check and
        the increment happen under one lock (in-memory) or one conditional
        upsert (Postgres), so concurrent requests cannot both pass.
        """
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        limit = tenant.quotas.get("max_executions_per_day", -1)
        today = datetime.now(UTC).date()
        if self._pg is not None:
            with self._pg.connection() as conn:
                if limit >= 0:
                    row = conn.execute(
                        "INSERT INTO vex_tenant_usage (tenant_id, day, count) "
                        "VALUES (%s, %s, 1) "
                        "ON CONFLICT (tenant_id, day) DO UPDATE "
                        "SET count = vex_tenant_usage.count + 1 "
                        "WHERE vex_tenant_usage.count < %s RETURNING count",
                        (tenant_id, today, limit)).fetchone()
                    return row is not None
                conn.execute(
                    "INSERT INTO vex_tenant_usage (tenant_id, day, count) "
                    "VALUES (%s, %s, 1) "
                    "ON CONFLICT (tenant_id, day) DO UPDATE "
                    "SET count = vex_tenant_usage.count + 1",
                    (tenant_id, today))
            return True
        with TenantService._quota_lock:
            if limit >= 0 and self.executions_today(tenant_id) >= limit:
                return False
            self.record_execution(tenant_id)
            return True

    # === Quota checks ===

    def check_quota(self, tenant_id: str, resource: str, current_usage: int = 0) -> bool:
        """Checks whether current usage is within quota."""
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return False
        quota_key = f"max_{resource}"
        if quota_key not in tenant.quotas:
            return True
        limit = tenant.quotas[quota_key]
        if limit < 0:  # No limit
            return True
        return current_usage < limit

    # === Postgres persistence (write-through; dicts stay as cache) ===

    def _load_pg(self) -> None:
        assert self._pg is not None
        with self._pg.connection() as conn:
            for r in conn.execute(
                    "SELECT tenant_id, name, tier, status, created_at, "
                    "updated_at, expires_at, settings, metadata "
                    "FROM vex_tenant").fetchall():
                self._tenants[r[0]] = Tenant(
                    tenant_id=r[0], name=r[1],
                    tier=TenantTier(r[2]), status=TenantStatus(r[3]),
                    created_at=r[4], updated_at=r[5], expires_at=r[6],
                    settings=r[7] or {}, metadata=r[8] or {})
            for r in conn.execute(
                    "SELECT member_id, tenant_id, user_id, roles, "
                    "joined_at, invited_by, is_active "
                    "FROM vex_tenant_member").fetchall():
                m = TenantMember(
                    member_id=r[0], tenant_id=r[1], user_id=r[2],
                    roles={TenantRole(x) for x in (r[3] or [])},
                    joined_at=r[4], invited_by=r[5], is_active=r[6])
                self._members.setdefault(r[1], []).append(m)
                self._user_to_tenant[r[2]] = r[1]
            for r in conn.execute(
                    "SELECT agent_id, tenant_id FROM vex_tenant_agent"
                    ).fetchall():
                self._agent_to_tenant[r[0]] = r[1]

    def _save_tenant(self, t: Tenant) -> None:
        if self._pg is None:
            return
        from psycopg.types.json import Jsonb
        with self._pg.connection() as conn:
            conn.execute(
                "INSERT INTO vex_tenant (tenant_id, name, tier, status, "
                "created_at, updated_at, expires_at, settings, metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "name=EXCLUDED.name, tier=EXCLUDED.tier, "
                "status=EXCLUDED.status, updated_at=EXCLUDED.updated_at, "
                "expires_at=EXCLUDED.expires_at, "
                "settings=EXCLUDED.settings, metadata=EXCLUDED.metadata",
                (t.tenant_id, t.name, t.tier.value, t.status.value,
                 t.created_at, t.updated_at, t.expires_at,
                 Jsonb(t.settings), Jsonb(t.metadata)))

    def _save_member(self, m: TenantMember) -> None:
        if self._pg is None:
            return
        from psycopg.types.json import Jsonb
        with self._pg.connection() as conn:
            conn.execute(
                "INSERT INTO vex_tenant_member (member_id, tenant_id, "
                "user_id, roles, joined_at, invited_by, is_active) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (member_id) DO UPDATE SET "
                "roles=EXCLUDED.roles, is_active=EXCLUDED.is_active",
                (m.member_id, m.tenant_id, m.user_id,
                 Jsonb(sorted(r.value for r in m.roles)),
                 m.joined_at, m.invited_by, m.is_active))

    def _delete_member(self, tenant_id: str, user_id: str) -> None:
        if self._pg is None:
            return
        with self._pg.connection() as conn:
            conn.execute(
                "DELETE FROM vex_tenant_member "
                "WHERE tenant_id = %s AND user_id = %s",
                (tenant_id, user_id))

    def _save_agent(self, agent_id: str, tenant_id: str) -> None:
        if self._pg is None:
            return
        with self._pg.connection() as conn:
            conn.execute(
                "INSERT INTO vex_tenant_agent (agent_id, tenant_id) "
                "VALUES (%s,%s) "
                "ON CONFLICT (agent_id) DO UPDATE SET "
                "tenant_id=EXCLUDED.tenant_id",
                (agent_id, tenant_id))

    def _delete_agent(self, agent_id: str) -> None:
        if self._pg is None:
            return
        with self._pg.connection() as conn:
            conn.execute(
                "DELETE FROM vex_tenant_agent WHERE agent_id = %s",
                (agent_id,))

    def _log_tenant_event(self, event_type: str, tenant_id: str, data: dict) -> None:
        if self._store:
            self._store.append(EventDraft(
                type=EventType.SYSTEM_CHECKPOINT,
                payload={"event": event_type, "tenant_id": tenant_id, **data}
            ), stream="tenancy")


# === Middleware/Helpers for FastAPI integration ===

class TenantContext:
    """Tenant context for the current request (thread-local via contextvar)."""
    _current: contextvars.ContextVar[TenantContext | None] = \
        contextvars.ContextVar("tenant_context", default=None)

    def __init__(self, tenant: Tenant, member: TenantMember | None = None):
        self.tenant = tenant
        self.member = member

    @classmethod
    def get(cls) -> TenantContext | None:
        return cls._current.get()

    @classmethod
    def set(cls, ctx: TenantContext | None) -> None:
        cls._current.set(ctx)

    @classmethod
    def require(cls) -> TenantContext:
        ctx = cls.get()
        if ctx is None:
            raise RuntimeError("No active tenant context")
        return ctx


import contextvars


def require_tenant_permission(permission: str):
    """FastAPI dependency that checks permissions of the current tenant."""
    from fastapi import Depends, HTTPException

    def _check(ctx: TenantContext = Depends(TenantContext.require)):
        if not ctx.member or not ctx.member.has_permission(permission):
            raise HTTPException(403, f"Permission required: {permission}")
        return ctx
    return _check


def get_current_tenant() -> TenantContext:
    """Dependency to get the current tenant."""
    return TenantContext.require()