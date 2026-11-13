"""Vexinter service: §12.2 (self-hosted) and §12.3 (managed).

DATA plane, signed by agent credential (§9 applied to
transport): tasks, invocations, verification, messages.

CONTROL plane, admin token or dev mode (§13.4, the
future console): identity, queues, revisions, checkpoints, metrics.

Environment configuration:
  VEXINTER_PG_DSN         PostgreSQL DSN (if absent, in-memory store)
  VEXINTER_AUTO_MIGRATE   "1" (default) applies the schema on startup
  VEXINTER_ADAPTERS       "pkg.module:Class,other:factory"
  VEXINTER_ADMIN_TOKEN    control plane token (without it: dev mode)
  VEXINTER_DEV_MODE       "1" disables authentication (DEV ONLY)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from vexinter.core.conditions import (
    ConditionError,
    EvalContext,
    snapshot_key,
    substitute_refs,
    validate_condition,
)
from vexinter.core.crypto import canonical_json, sha256_hex
from vexinter.core.events import (
    EventDraft,
    EventType,
    new_id,
)
from vexinter.core.graph import execution_graph
from vexinter.core.metrics import compute_metrics
from vexinter.core.multiagent import SubTaskLimitError
from vexinter.core.observability import (
    HealthStatus,
    get_correlation_id,
    log_audit_event,
    set_correlation_id,
)
from vexinter.core.policy import PolicyDecision, PolicyRule
from vexinter.core.reconstruct import ServerRuntime
from vexinter.core.recovery import StateError
from vexinter.core.statemachine import (
    ExecutionState,
    StateTransitionError,
    state_from_verdict,
)
from vexinter.core.store import EventQuery, InMemoryEventStore
from vexinter.core.taskrun import TaskRun
from vexinter.core.tools import (
    AmbiguousInvocationError,
    AuthorizationError,
    CallContext,
    ExecutionLimitError,
    IndeterminateOutcome,
    UnknownToolError,
)
from vexinter.core.verifier import VerdictResult
from vexinter.identity import OperatorRole, Rejection, b64d

MAX_SKEW_S = 300.0


class TaskBody(BaseModel):
    agent: str | None = None
    objective: str
    condition: dict
    input: dict = Field(default_factory=dict)
    snapshot: list[dict] = Field(default_factory=list)
    # Idempotent creation: a client retrying after a network cut reuses
    # the same key and gets the SAME task back — no double quota.
    idempotency_key: str | None = Field(default=None, max_length=200)
    # FinOps: per-task budget and execution limits. max_steps/deadline_s
    # override the conservative service defaults (VEXINTER_MAX_STEPS,
    # VEXINTER_TASK_DEADLINE_S) explicitly per task.
    max_tokens: int | None = None
    max_usd: float | None = None
    model_pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    max_steps: int | None = None
    deadline_s: float | None = None
    loop_threshold: int | None = None
    cycle_threshold: int | None = None
    no_progress_window: int | None = None


class InvokeBody(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    agent: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, max_length=200)


class RecoverBody(BaseModel):
    failed_tool: str | None = None
    invoked_event: str | None = None


class HITLDecideBody(BaseModel):
    """Human decision for an HITL case."""

    case_id: str
    action: str  # "retry" | "compensate" | "reverify" | "close"
    operator: str | None = None


class CompleteBody(BaseModel):
    exception: bool = False
    last_status: str | None = None


class PlanBody(BaseModel):
    plan: dict


class EventBody(BaseModel):
    type: str
    payload: dict | None = None
    task_id: str | None = None
    execution_id: str | None = None
    agent_id: str | None = None
    operation_id: str | None = None
    caused_by: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class EnvelopeBody(BaseModel):
    envelope: dict


class AckBody(BaseModel):
    message_id: str
    body_hash: str


class EnrollBody(BaseModel):
    agent_id: str
    display_name: str = ""
    allowed_tools: list[str] | None = None
    allowed_recipients: list[str] | None = None


class InstanceBody(BaseModel):
    agent_id: str


class CredIssueBody(BaseModel):
    instance_id: str
    signing_pub: str
    encryption_pub: str


class RotateBody(BaseModel):
    signing_pub: str
    encryption_pub: str


class RevokeBody(BaseModel):
    reason: str = ""


class DecideBody(BaseModel):
    action: str
    operator: str = "operator"


class EraseBody(BaseModel):
    execution_ids: list[str] = Field(min_length=1, max_length=500)
    reason: str = ""


class GdprErasureBody(BaseModel):
    person_id: str = Field(min_length=1, max_length=200)
    requested_by: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class GdprVerifyBody(BaseModel):
    verification_token: str = Field(min_length=1, max_length=200)


class TenantBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tier: str = "free"
    owner_user_id: str | None = None


class TenantMemberBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(default_factory=lambda: ["viewer"])


class TenantAgentBody(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)


class CollaboratorBody(BaseModel):
    agent: str


class MCPInvokeBody(BaseModel):
    system: str
    tool: str
    args: dict = Field(default_factory=dict)


class PolicyRuleBody(BaseModel):
    id: str
    name: str
    description: str
    effect: str  # "allow", "deny", "require_approval"
    conditions: dict = Field(default_factory=dict)
    priority: int = 0


class RedTeamRunBody(BaseModel):
    scenario: str
    system: str
    tool: str
    args: dict = Field(default_factory=dict)


class AuthLoginBody(BaseModel):
    token: str
    args: dict = Field(default_factory=dict)


class RedTeamSuiteBody(BaseModel):
    system: str
    tool: str
    args: dict = Field(default_factory=dict)
    critical_only: bool = False


class MemoryLessonsBody(BaseModel):
    tags: dict | None = None
    min_confidence: float | None = None


class MemorySearchBody(BaseModel):
    query: str
    top_k: int = 10
    min_score: float = 0.1


class SubTaskDelegateBody(BaseModel):
    agent_id: str
    objective: str
    condition: dict
    input: dict | None = None


class AgentMessageBody(BaseModel):
    to_agent: str
    content: dict
    message_type: str = "notification"


class SequentialDelegationBody(BaseModel):
    steps: list[dict]


class ParallelDelegationBody(BaseModel):
    steps: list[dict]


class NotifyAgentsBody(BaseModel):
    to_agents: list[str]
    content: dict
    message_type: str = "notification"


def _adapter_allowed_prefixes() -> tuple[str, ...]:
    """First-party namespaces plus any extra prefixes the operator
    explicitly allows via VEXINTER_ADAPTER_PREFIXES (comma-separated).
    A compromised VEXINTER_ADAPTERS alone cannot load arbitrary modules."""
    extra = os.environ.get("VEXINTER_ADAPTER_PREFIXES", "")
    prefixes = ["vexinter.", "examples."]
    prefixes += [x.strip() for x in extra.split(",") if x.strip()]
    return tuple(prefixes)


def _load_adapters(spec: str | None):
    """ "pkg.mod:Class" or "pkg.mod:factory" (returns adapter or list)."""
    if not spec:
        return []
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        mod_name, _, attr = part.partition(":")
        mod_name = mod_name.strip()
        allowed = _adapter_allowed_prefixes()
        if not mod_name.startswith(allowed):
            raise ValueError(
                f"adapter module '{mod_name}' not allowed; "
                f"must start with one of {allowed} "
                "(extend via VEXINTER_ADAPTER_PREFIXES)"
            )
        obj = getattr(importlib.import_module(mod_name), attr.strip())
        produced = obj()
        out.extend(produced if isinstance(produced, list) else [produced])
    return out


_NOMBRE_OPERADOR = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _check_operators(ops: dict[str, dict]) -> dict[str, dict]:
    """Validates operator tokens and returns dict with metadata."""
    vistos: set[str] = set()
    for nombre, meta in ops.items():
        if not _NOMBRE_OPERADOR.match(nombre or ""):
            raise ValueError(f"invalid operator name: {nombre!r}")
        if nombre == "admin":
            raise ValueError(
                "the 'admin' name is reserved for the admin token"
            )

        token = meta["token"]
        if len(token or "") < 12:
            raise ValueError(
                f"token for {nombre} is too short (minimum 12 characters)"
            )
        if token in vistos:
            raise ValueError("two operators cannot share the same token")
        vistos.add(token)

        # Validate expiration (accepts datetime or ISO8601 str)
        expires_at = meta.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except ValueError:
                raise ValueError(
                    f"invalid expiration date for operator '{nombre}'"
                )
        # A naive ISO timestamp is assumed UTC — comparing it against an
        # aware datetime would raise TypeError on every request.
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and expires_at < datetime.now(UTC):
            raise ValueError(f"token for operator '{nombre}' has already expired")

    return dict(ops)


def _parse_operators(spec: str | None) -> dict[str, dict]:
    """
    Parses operators with extended format:
    'ana:token1:viewer:2025-12-31T23:59:59Z,luis:token2:operator'

    Format: name:token[:role[:expires_at]]
    - role: viewer | console_admin | operator | admin (default: operator)
    - expires_at: ISO8601 UTC (default: no expiration)

    Returns dict[name] = {token, role, expires_at}
    """
    from vexinter.identity import OperatorRole

    ops: dict[str, dict] = {}
    for parte in (spec or "").split(","):
        parte = parte.strip()
        if not parte:
            continue

        # maxsplit=3: expires_at is ISO8601 and contains ':' (23:59:59)
        parts = parte.split(":", 3)
        if len(parts) < 2:
            raise ValueError(
                "malformed operator (expected name:token[:role[:expires]])"
            )

        nombre = parts[0].strip()
        token = parts[1].strip()
        role = parts[2].strip() if len(parts) > 2 else "operator"
        expires_at = parts[3].strip() if len(parts) > 3 else None

        try:
            role_enum = OperatorRole(role)
        except ValueError:
            raise ValueError(
                f"invalid role '{role}' for operator '{nombre}': "
                f"allowed values: {[r.value for r in OperatorRole]}"
            )

        expires_dt = None
        if expires_at:
            try:
                expires_dt = datetime.fromisoformat(expires_at)
            except ValueError:
                raise ValueError(
                    f"invalid expiration date '{expires_at}' for operator '{nombre}'"
                )
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=UTC)

        if nombre in ops:
            raise ValueError(f"duplicate operator: {nombre}")

        ops[nombre] = {"token": token, "role": role_enum, "expires_at": expires_dt}

    return _check_operators(ops)


def make_app(
    *,
    store=None,
    adapters=None,
    dev_mode=None,
    admin_token=None,
    checkpoint_signer=None,
    followup_delay_s: float = 30.0,
    console: bool = True,
    operators: dict[str, str | dict] | None = None,
) -> FastAPI:
    dev_mode = (
        os.environ.get("VEXINTER_DEV_MODE") == "1" if dev_mode is None else dev_mode
    )
    admin_token = admin_token or os.environ.get("VEXINTER_ADMIN_TOKEN")

    # Normalize operators to new format: dict[str, dict] with token, role, expires_at
    if operators is not None:
        normalized: dict[str, dict] = {}
        for name, value in operators.items():
            if isinstance(value, str):
                # Old format: convert to new format
                normalized[name] = {
                    "token": value,
                    "role": OperatorRole.OPERATOR,
                    "expires_at": None,
                }
            elif isinstance(value, dict):
                # Already new format
                normalized[name] = value
            else:
                raise TypeError(f"invalid format for operator '{name}'")
        ops_norm = _check_operators(normalized)
    else:
        ops_norm = _parse_operators(os.environ.get("VEXINTER_OPERATOR_TOKENS"))
    if admin_token:
        # Check against token values in operators dict (which has structure name -> {token, role, expires_at})
        if admin_token in [meta["token"] for meta in ops_norm.values()]:
            raise ValueError(
                "the admin token cannot be repeated among the operators"
            )
        ops_norm = {
            **ops_norm,
            "admin": {
                "token": admin_token,
                "role": OperatorRole.ADMIN,
                "expires_at": None,
            },
        }
    if store is None:
        dsn = os.environ.get("VEXINTER_PG_DSN")
        if dsn:
            from vexinter.store_pg import PostgresEventStore, init_schema

            store = PostgresEventStore(dsn)
            if os.environ.get("VEXINTER_AUTO_MIGRATE", "1") == "1":
                init_schema(store)
        else:
            store = InMemoryEventStore()

    keyring_dir = os.environ.get("VEXINTER_KEYRING_DIR")
    if keyring_dir:
        from vexinter.privacy import FileKeyRing, PrivacyStore

        if not isinstance(store, PrivacyStore):
            store = PrivacyStore(store, FileKeyRing(keyring_dir))

    # Paid-tier plugin: vexinter-pro (private package) registers the
    # paid tiers and provides the license client. Community Edition
    # runs identically without it — paid plans simply degrade to
    # community limits.
    license_client = None
    try:
        import vexinter_pro
        license_client = vexinter_pro.client_from_env()
    except ImportError:
        pass
    from vexinter.core.multitenancy import TenantContext, TenantService

    tenant_svc = TenantService(store)
    seq_guard = InMemorySequenceGuard_safe(store)
    try:
        from vexinter.store_pg import PostgresEventStore as _PGStore
    except ImportError:
        _is_pg = False
    else:
        _is_pg = isinstance(store, _PGStore)
    if not _is_pg:
        if os.environ.get("VEXINTER_REQUIRE_PG") == "1":
            raise RuntimeError(
                "VEXINTER_REQUIRE_PG=1 but the event store is not "
                "Postgres: in-memory anti-replay is per-process and "
                "unsafe with multiple workers")
        logging.getLogger(__name__).warning(
            "in-memory sequence guard: anti-replay is per-process; "
            "run a single worker or use PostgresEventStore for "
            "multi-worker deployments"
        )
    rt = ServerRuntime(
        adapters
        if adapters is not None
        else _load_adapters(os.environ.get("VEXINTER_ADAPTERS")),
        store,
        followup_delay_s=followup_delay_s,
        authenticated=not dev_mode,
        credential_ttl_s=float(os.environ.get("VEXINTER_CREDENTIAL_TTL_S", "3600")),
        rotation_grace_s=float(os.environ.get("VEXINTER_ROTATION_GRACE_S", "900")),
    )

    app = FastAPI(
        title="Vexinter", version="0.5.1", debug=os.environ.get("VEXINTER_DEBUG") == "1"
    )
    app.state.runtime = rt
    from .console import LoginLimiter
    app.state.login_limiter = LoginLimiter(
        max_fails=int(os.environ.get("VEXINTER_LOGIN_MAX_FAILS", "5")),
        lock_s=float(os.environ.get("VEXINTER_LOGIN_LOCK_S", "900")))

    # Phase 8: Production Hardening - Health checks
    @app.get("/", include_in_schema=False)
    def root():
        """API root: no content here — send browsers to the console."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/console")

    @app.get("/health")
    def health():
        """Liveness probe - process is alive."""
        return {"status": "healthy", "service": "vexinter"}

    @app.get("/ready")
    def ready():
        """Readiness probe - ready to receive traffic."""
        hc = rt.health_checker.check_readiness()
        status_code = 200 if hc.status == HealthStatus.HEALTHY else 503
        return Response(
            content=json.dumps(
                {
                    "status": hc.status.value,
                    "message": hc.message,
                    "checks": [
                        {
                            "name": c.name,
                            "status": c.status.value,
                            "message": c.message,
                            "latency_ms": c.latency_ms,
                        }
                        for c in [hc]
                    ],
                }
            ),
            status_code=status_code,
            media_type="application/json",
        )

    @app.get("/live")
    def live():
        """Startup probe - initialization completed."""
        hc = rt.health_checker.check_liveness()
        return {"status": hc.status.value, "message": hc.message}

    @app.get("/health/full")
    def full_health():
        """Full health check with all components."""
        health = rt.health_checker.full_health()
        status_code = 200 if health.status == HealthStatus.HEALTHY else 503
        return Response(
            content=json.dumps(
                {
                    "status": health.status.value,
                    "version": health.version,
                    "timestamp": health.timestamp.isoformat(),
                    "uptime_seconds": health.uptime_seconds,
                    "checks": [
                        {
                            "name": c.name,
                            "status": c.status.value,
                            "message": c.message,
                            "latency_ms": c.latency_ms,
                            "details": c.details,
                        }
                        for c in health.checks
                    ],
                }
            ),
            status_code=status_code,
            media_type="application/json",
        )

    # Phase 8: Request tracing middleware
    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID", get_correlation_id())
        set_correlation_id(correlation_id)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    # Phase 8: Rate limiting middleware
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path in {
            "/health",
            "/ready",
            "/live",
            "/health/full",
            "/metrics",
        }:
            return await call_next(request)

        # Get client identifier
        client_id = request.headers.get("X-Vex-Credential", "anonymous")
        allowed, info = rt.rate_limiter.allow(client_id)

        if not allowed:
            log_audit_event(
                "rate_limit_exceeded",
                client_id,
                "api",
                "request",
                "rejected",
                {"retry_after": info["retry_after"]},
                get_correlation_id(),
            )
            return Response(
                content=json.dumps(
                    {"error": "rate limit exceeded", "retry_after": info["retry_after"]}
                ),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(int(info["retry_after"]))},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(info["remaining"])
        if info["retry_after"] > 0:
            response.headers["Retry-After"] = str(int(info["retry_after"]))
        return response

    async def principal(request: Request):
        if dev_mode:
            return None
        cred_id = request.headers.get("X-Vex-Credential")
        ts_h = request.headers.get("X-Vex-Timestamp")
        seq_h = request.headers.get("X-Vex-Sequence")
        sig = request.headers.get("X-Vex-Signature")
        if not all([cred_id, ts_h, seq_h, sig]):
            raise HTTPException(401, "missing authentication headers")
        try:
            cred = rt.identity.assert_credential_valid(cred_id)
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(401, f"invalid credential: {e}")

        # Tenant enforcement: agents bound to a tenant inherit its status.
        tenant = tenant_svc.tenant_of_agent(cred.agent_id)
        if tenant is not None:
            if not tenant.is_active:
                raise HTTPException(403, "tenant is suspended or expired")
            TenantContext.set(TenantContext(tenant))

        body = await request.body()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive

        material = (
            f"{request.method}|{request.url.path}|"
            f"{hashlib.sha256(body).hexdigest()}|{ts_h}|{seq_h}"
        )
        try:
            Ed25519PublicKey.from_public_bytes(b64d(cred.signing_pub or "")).verify(
                b64d(sig or ""), material.encode()
            )
        except (InvalidSignature, ValueError):
            raise HTTPException(401, "invalid signature")
        try:
            if abs(time.time() - float(ts_h or "")) > MAX_SKEW_S:
                raise HTTPException(401, "timestamp outside window")
            if not seq_guard.check_and_advance(cred_id, int(seq_h or "")):
                raise HTTPException(401, "replay or non-increasing sequence")
        except ValueError:
            raise HTTPException(401, "invalid numeric headers")
        return cred

    # SPA session via HttpOnly cookie (same signed scheme as the console)
    from .console import SESSION_TTL_S as _SESSION_TTL_S
    from .console import _issue as _issue_spa_cookie
    from .console import _session as _spa_session
    from .console import client_ip as _client_ip

    SPA_COOKIE = "vex_spa"
    # Secure by default (TLS deployments); set VEXINTER_COOKIE_SECURE=0
    # only for local development over plain HTTP.
    _cookie_secure = os.environ.get("VEXINTER_COOKIE_SECURE", "1") != "0"
    _samesite_raw = os.environ.get("VEXINTER_COOKIE_SAMESITE", "strict").lower()
    _cookie_samesite = cast(
        "Literal['strict', 'lax', 'none']",
        _samesite_raw if _samesite_raw in ("strict", "lax", "none")
        else "strict")

    def _spa_tokens() -> dict[str, str]:
        """name -> token of current operators (excluding expired ones)."""
        now = datetime.now(UTC)
        return {
            n: m["token"] for n, m in ops_norm.items()
            if not (m.get("expires_at") and m["expires_at"] < now)
        }

    def require_admin(request: Request, x_vex_admin: str | None = Header(None)):
        """Returns the name of the operator that presented the token (None in
        dev mode). Accepts the X-Vex-Admin header or the SPA's HttpOnly
        session cookie. Compares against all tokens without short-circuiting
        on the first match, so the timing does not reveal which one matched."""
        if dev_mode:
            return None
        name = _spa_session(_spa_tokens(), request.cookies.get(SPA_COOKIE))
        if name is not None:
            return name
        dado = (x_vex_admin or "").encode()
        elegido = None
        for nombre, meta in ops_norm.items():
            token = meta["token"]
            expires_at = meta.get("expires_at")
            if expires_at and expires_at < datetime.now(UTC):
                continue  # Expired token, skip
            if hmac.compare_digest(dado, token.encode()):
                elegido = nombre
        if elegido is None:
            raise HTTPException(403, "admin token required")
        return elegido

    @app.post("/v1/auth/login", status_code=204)
    async def spa_login(request: Request, b: AuthLoginBody):
        """Exchanges the operator token for an HttpOnly session cookie.

        The token does not return to the browser as a reusable credential:
        the cookie only carries name.expiry.HMAC signature (the console's
        scheme), so an XSS cannot steal the token itself.
        """
        if dev_mode:
            resp = Response(status_code=204)
            resp.set_cookie(SPA_COOKIE, _issue_spa_cookie("dev", "dev"),
                            httponly=True, samesite=_cookie_samesite,
                            secure=_cookie_secure, max_age=_SESSION_TTL_S)
            return resp
        limiter = getattr(request.app.state, "login_limiter", None)
        origen = _client_ip(request)
        if limiter is not None:
            espera = limiter.blocked(origen)
            if espera > 0:
                raise HTTPException(
                    429, f"too many attempts: try again in {int(espera) + 1} s")
        tokens = _spa_tokens()
        dado = b.token.encode()
        elegido = None
        for n, t in tokens.items():
            if hmac.compare_digest(dado, t.encode()):
                elegido = elegido or n  # no short-circuit: constant-time across all tokens
        if elegido is None:
            if limiter is not None:
                limiter.failure(origen)
            raise HTTPException(403, "invalid token")
        if limiter is not None:
            limiter.success(origen)
        resp = Response(status_code=204)
        resp.set_cookie(SPA_COOKIE, _issue_spa_cookie(tokens[elegido], elegido),
                        httponly=True, samesite=_cookie_samesite,
                        secure=_cookie_secure, max_age=_SESSION_TTL_S)
        return resp

    @app.get("/v1/auth/session")
    def spa_session(request: Request):
        """SPA session state (for frontend startup)."""
        if dev_mode:
            return {"authenticated": True, "operator": "dev"}
        name = _spa_session(_spa_tokens(), request.cookies.get(SPA_COOKIE))
        return {"authenticated": name is not None, "operator": name}

    @app.delete("/v1/auth/logout", status_code=204)
    def spa_logout():
        resp = Response(status_code=204)
        resp.delete_cookie(
            SPA_COOKIE, path="/", httponly=True,
            samesite=_cookie_samesite, secure=_cookie_secure,
        )
        return resp

    def _run_or_404(task_id: str) -> TaskRun:
        run = rt.runs.get(task_id)
        if run is not None:
            return run
        # Multi-worker / late-join: the task may have been created by
        # another worker after this process restored its run cache.
        # Rebuild a minimal TaskRun from the TASK_CREATED event — the
        # log is the source of truth.
        for e in rt.store.query(EventQuery(
                task_id=task_id,
                types={EventType.TASK_CREATED}, limit=1)):
            p = e.payload or {}
            if p.get("_redacted") or p.get("_redactado"):
                break
            run = TaskRun(task_id=e.task_id, execution_id=e.execution_id,
                      agent_id=e.agent_id or "",
                      objective=p.get("objective", ""),
                      condition=p.get("condition") or {},
                      input=p.get("input") or {},
                      instance_id=p.get("instance_id"),
                      credential_id=p.get("credential_id"))
            rt.runs[run.task_id] = run
            # Same restore as reconstruct._restore_runs: a completed
            # execution must not look active to this worker.
            for ce in rt.store.query(EventQuery(
                    execution_id=e.execution_id,
                    types={EventType.EXECUTION_COMPLETED}, limit=1)):
                run.completed = True
                break
            return run
        raise HTTPException(404, f"unknown task: {task_id}")

    def _require_owner(cred, run) -> None:
        if cred is not None and cred.agent_id != run.agent_id:
            raise HTTPException(403, "task belongs to another agent")

    def _do_verify(run: TaskRun) -> VerdictResult:
        """Exact replica of Task.verify (with its state machine)."""
        # Concurrent verifies are legitimate: the verdict is a pure read
        # of the log. State transitions are bookkeeping — if another
        # request already moved the execution, we still return OUR
        # verdict instead of failing with a 500.
        try:
            if rt.tracker.can(run.execution_id, ExecutionState.PENDING_VERIFICATION):
                rt.tracker.transition(
                    run, ExecutionState.PENDING_VERIFICATION,
                    "verification requested",
                )
        except StateTransitionError:
            pass
        v = rt.verifier.verify(run)
        rt.last_verdicts[run.execution_id] = v
        try:
            rt.tracker.transition(
                run, state_from_verdict(v.status, v.provisional),
                f"verdict: {v.status}",
            )
        except StateTransitionError:
            logging.getLogger(__name__).warning(
                "verify(%s): state already advanced; verdict %s still valid",
                run.execution_id, v.status,
            )
        if v.status == "inconclusive" or v.followup_required:
            rt.scheduler.schedule(
                run,
                rt.followup_delay_s,
                "inconclusive or provisional result (§4.4)",
            )
        return v

    # Tenant tier -> billing plan, for per-tenant quota buckets on
    # multi-tenant deployments. STARTER/FREE map to community: the
    # tenant quotas (executions/day, agents) already differentiate
    # them, and the monthly task bucket follows the billing plan.
    _TENANT_TIER_TO_PLAN = {
        "free": "community", "starter": "community",
        "professional": "professional", "enterprise": "business",
    }

    def _resolve_subscriber(_p):
        """Subscriber identity + plan, shared by quota and ceilings."""
        email = getattr(_p, "email", None) or os.environ.get(
            "VEXINTER_DEFAULT_SUBSCRIBER_EMAIL", "community@vexinter.com"
        )
        # A configured license is the ONLY source of paid plans: an
        # env var must never grant a paid tier without a verified
        # signed document. Env defaults apply only in dev mode.
        if license_client is not None:
            plan = license_client.get_plan() or "community"
        else:
            plan = (os.environ.get("VEXINTER_DEFAULT_PLAN", "community")
                    if dev_mode else "community")
        # Per-tenant override: an agent bound to a tenant inherits the
        # tenant's tier, and the tenant id keys the usage bucket —
        # hosted deployments get per-customer quotas instead of one
        # shared bucket for every request.
        agent_id = getattr(_p, "agent_id", None)
        tenant = (tenant_svc.tenant_of_agent(agent_id)
                  if agent_id else None)
        if tenant is not None:
            email = tenant.tenant_id
            plan = _TENANT_TIER_TO_PLAN.get(tenant.tier.value, "community")
        return email, plan

    def _license_max_instances():
        """Instance cap from the verified license payload, or None when
        unlimited / no license configured. Binds a paid plan to a
        bounded fleet instead of letting one seat stretch across
        unlimited deployments."""
        if license_client is None:
            return None
        try:
            from vexinter_pro.licensing import effective_plan
            payload = license_client.get_payload()
        except Exception:
            return None
        if not payload or effective_plan(payload) == "community":
            return None
        cap = payload.get("max_instances")
        return int(cap) if cap and int(cap) > 0 else None

    def _consume_task_quota(email: str, plan: str):
        """Atomically checks + records one task unit. Raises 403 over
        quota. Called ONLY for accepted tasks: anything rejected during
        validation, idempotent replay or tenant checks returns before
        this point and never consumes quota."""
        from vexinter.core.tiers import get_tier_manager

        tier_mgr = get_tier_manager(store)
        allowed, denial_reason = tier_mgr.consume_task(email, plan)
        if not allowed:
            raise HTTPException(403, detail=denial_reason)

    def _env_limit(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, str(default))))
        except ValueError:
            return default

    task_create_lock = threading.Lock()

    @app.post("/v1/tasks")
    def create_task(b: TaskBody, _p=Depends(principal)):
        if len(b.model_dump_json()) > 262_144:
            raise HTTPException(422, "task payload too large (256 KiB max)")
        try:
            validate_condition(b.condition)
        except ConditionError as e:
            raise HTTPException(422, f"invalid condition: {e}")
        agent = _p.agent_id if _p is not None else b.agent
        if agent is None:
            raise HTTPException(400, "agent or credential required")

        # Serialized so two concurrent retries of the same
        # idempotency_key cannot both pass the dedup check.
        with task_create_lock:
            return _create_task(b, _p, agent)

    def _create_task(b: TaskBody, _p, agent: str):
        subscriber_email, subscriber_plan = _resolve_subscriber(_p)

        # Idempotent creation: a client retrying after a network cut
        # reuses idempotency_key and gets the SAME task back — the quota
        # unit was already consumed by the first accepted request.
        idem_scope = request_hash = None
        if b.idempotency_key:
            idem_scope = sha256_hex(f"{agent}|{b.idempotency_key}")
            request_hash = sha256_hex(canonical_json(
                b.model_dump(mode="json",
                             exclude={"idempotency_key"})))
            prior = rt.store.query(EventQuery(
                types={EventType.TASK_CREATED},
                payload_eq={"idem_scope": idem_scope}, limit=1))
            if prior:
                prev = prior[0].payload or {}
                if prev.get("request_hash") == request_hash:
                    return {"task_id": prior[0].task_id,
                            "execution_id": prior[0].execution_id,
                            "deduplicated": True}
                raise HTTPException(
                    409, "idempotency_key already used with a different "
                         "task payload")

        # Tenant quota first: a task rejected here must not consume the
        # subscriber's monthly task quota either.
        tenant = tenant_svc.tenant_of_agent(agent)
        if tenant is not None and not tenant_svc.try_record_execution(tenant.tenant_id):
            raise HTTPException(429, "daily execution quota exceeded for tenant")

        _consume_task_quota(subscriber_email, subscriber_plan)

        # Quota visibility: warn early (>=80%) so operators can upgrade
        # or throttle before the hard block rejects new tasks. Tasks
        # already running are unaffected — the block only applies to
        # NEW task creation.
        warnings_quota = None
        try:
            from vexinter.core.tiers import get_tier_manager as _gtm
            _summary = _gtm(store).get_usage_summary(
                subscriber_email, subscriber_plan)
            _used, _limit = (_summary["tasks_used"],
                             _summary["task_limit"])
            if _limit and _used / _limit >= 0.8:
                warnings_quota = (
                    f"monthly quota {_used}/{_limit} tasks used "
                    f"({round(_used / _limit * 100)}%) — new tasks are "
                    "rejected with QUOTA_EXCEEDED once the limit is "
                    "reached; running tasks are unaffected")
        except Exception:
            warnings_quota = None

        # Per-task budget + execution limits. max_tokens is the primary
        # bound (max_usd depends on client-supplied model_pricing and is
        # only as reliable as that table). Community gets a hard ceiling;
        # other plans fall back to the service default when unset.
        from decimal import Decimal

        from vexinter.models.budget import Budget

        warnings: list[str] = []
        if warnings_quota:
            warnings.append(warnings_quota)
        default_max_tokens = _env_limit("VEXINTER_DEFAULT_MAX_TOKENS",
                                        100000)
        has_client_budget = any((
            b.max_tokens is not None, b.max_usd is not None,
            bool(b.model_pricing), b.max_steps is not None,
            b.deadline_s is not None, b.loop_threshold is not None,
            b.cycle_threshold is not None,
            b.no_progress_window is not None))
        if subscriber_plan == "community":
            ceiling = _env_limit("VEXINTER_COMMUNITY_MAX_TOKENS", 50000)
            requested = (b.max_tokens if b.max_tokens is not None
                         else ceiling)
            eff_max_tokens = min(requested, ceiling)
            if b.max_tokens is not None and b.max_tokens > ceiling:
                warnings.append(
                    f"max_tokens capped at the Community ceiling "
                    f"({ceiling}); self-host for higher limits")
        else:
            eff_max_tokens = (b.max_tokens if b.max_tokens is not None
                              else default_max_tokens)
        if not has_client_budget:
            warnings.append(
                "no explicit budget: service defaults apply "
                f"(max_tokens={eff_max_tokens}, "
                f"max_steps={_env_limit('VEXINTER_MAX_STEPS', 100)}, "
                f"deadline_s={float(os.environ.get('VEXINTER_TASK_DEADLINE_S', '1800'))})")
        budget = Budget(
            max_tokens=eff_max_tokens,
            max_usd=Decimal(str(b.max_usd)) if b.max_usd else None,
            model_pricing={m: {k: Decimal(str(v)) for k, v in p.items()}
                           for m, p in b.model_pricing.items()},
            max_steps=b.max_steps,
            deadline_s=b.deadline_s,
            loop_threshold=b.loop_threshold,
            cycle_threshold=b.cycle_threshold,
            no_progress_window=b.no_progress_window,
        )
        run = TaskRun(
            task_id=new_id("task"),
            execution_id=new_id("exec"),
            agent_id=agent,
            objective=b.objective,
            condition=b.condition,
            input=b.input,
            instance_id=_p.instance_id if _p else None,
            credential_id=_p.credential_id if _p else None,
            budget=budget,
        )
        rt.store.append(
            EventDraft(
                type=EventType.TASK_CREATED,
                task_id=run.task_id,
                execution_id=run.execution_id,
                agent_id=run.agent_id,
                payload={
                    "objective": run.objective,
                    "condition": run.condition,
                    "input": run.input,
                    "instance_id": run.instance_id,
                    "credential_id": run.credential_id,
                    "budget": run.budget.model_dump() if run.budget else None,
                    "idem_scope": idem_scope,
                    "request_hash": request_hash,
                    "warnings": warnings,
                },
            ),
            stream=run.stream,
        )
        rt.store.append(
            EventDraft(
                type=EventType.EXECUTION_STARTED,
                task_id=run.task_id,
                execution_id=run.execution_id,
            ),
            stream=run.stream,
        )
        ectx = EvalContext(run.input, {})
        for s in b.snapshot:
            q = substitute_refs(s["query"], ectx)
            obs = rt.router.observe(s["system"], q)
            run.pre_snapshot[snapshot_key(s["system"], q)] = obs
            rt.store.append(
                EventDraft(
                    type=EventType.STATE_OBSERVED,
                    task_id=run.task_id,
                    execution_id=run.execution_id,
                    payload={
                        "tag": "pre",
                        "system": s["system"],
                        "query": q,
                        "observed": obs.data,
                        "consistency": obs.consistency,
                    },
                ),
                stream=run.stream,
            )
        rt.runs[run.task_id] = run
        resp = {"task_id": run.task_id, "execution_id": run.execution_id}
        if warnings:
            resp["warnings"] = warnings
        return resp

    @app.post("/v1/tasks/{task_id}/invoke")
    def invoke_tool(task_id: str, b: InvokeBody, _p=Depends(principal)):
        run = _run_or_404(task_id)
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(
                403,
                "task belongs to another agent and its owner did not "
                "authorize it as a collaborator",
            )
        if run.completed:
            raise HTTPException(409, "execution already completed")
        agent = _p.agent_id if _p is not None else b.agent
        if agent is None:
            raise HTTPException(400, "agent or credential required")
        ctx = CallContext(
            agent_id=agent,
            instance_id=_p.instance_id if _p else None,
            credential_id=_p.credential_id if _p else None,
            task_id=run.task_id,
            execution_id=run.execution_id,
            stream=run.stream,
            budget=run.budget,
        )
        client_key = None
        if b.idempotency_key:
            client_key = sha256_hex(
                f"{run.execution_id}|{b.idempotency_key}|{b.tool}|"
                f"{canonical_json(b.args)}"
            )
        try:
            res = rt.intermediary.call(
                ctx, b.tool, b.args, depends_on=b.depends_on, client_key=client_key
            )
        except AuthorizationError as e:
            raise HTTPException(403, str(e))
        except UnknownToolError as e:
            raise HTTPException(404, str(e))
        except AmbiguousInvocationError as e:
            raise HTTPException(409, str(e))
        except IndeterminateOutcome as e:
            raise HTTPException(502, f"indeterminate outcome: {e}")
        except ExecutionLimitError as e:
            raise HTTPException(429, str(e))
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(422, str(e))
        return {
            "ok": res.ok,
            "data": res.data,
            "error": res.error,
            "provenance": res.provenance.model_dump(),
        }

    @app.post("/v1/tasks/{task_id}/verify")
    def verify_task(task_id: str, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        return _do_verify(run).model_dump(mode="json")

    @app.post("/v1/tasks/{task_id}/recover")
    def recover_task(task_id: str, b: RecoverBody, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        v = rt.last_verdicts.get(run.execution_id) or _do_verify(run)
        try:
            case = rt.recovery.handle_discrepancy(
                run, v, failed_tool=b.failed_tool, invoked_event=b.invoked_event
            )
        except AuthorizationError as e:
            raise HTTPException(403, str(e))
        except IndeterminateOutcome as e:
            raise HTTPException(502, f"indeterminate outcome: {e}")
        return {"case": case.model_dump(mode="json") if case else None}

    @app.post("/v1/tasks/{task_id}/hitl/decide")
    def hitl_decide(
        task_id: str, b: HITLDecideBody, request: Request,
        x_vex_admin: str | None = Header(None),
        _p=Depends(principal),
    ):
        """Processes a human decision for an HITL case (§5.4).

        The operator is the authenticated admin (SPA session or
        X-Vex-Admin), never the calling agent — an agent cannot approve
        its own human-in-the-loop decision. In dev_mode there is no admin
        auth, so b.operator / "operator" is accepted for local testing.
        """
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        operator = require_admin(request, x_vex_admin)
        if operator is None:
            operator = b.operator or "operator"
        try:
            fresh = rt.human_queue.decide(b.case_id, b.action, operator)
        except KeyError:
            raise HTTPException(404, f"case not found: {b.case_id}")
        except StateError as e:
            raise HTTPException(409, str(e))

        # Emit HITL_RESOLVED event
        rt.store.append(
            EventDraft(
                type=EventType.HITL_RESOLVED,
                task_id=run.task_id,
                execution_id=run.execution_id,
                agent_id=_p.agent_id,
                payload={
                    "case_id": b.case_id,
                    "action": b.action,
                    "operator": operator,
                    "result": fresh.status,
                },
            ),
            stream=run.stream,
        )

        return {"verdict": fresh.model_dump(mode="json")}

    @app.get("/v1/tasks/{task_id}/hitl/pending")
    def hitl_pending(task_id: str, _p=Depends(principal)):
        """Lists HITL cases pending human decision."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        pending = rt.human_queue.pending()
        return {"pending": [c.model_dump(mode="json") for c in pending]}

    @app.get("/v1/tasks/{task_id}/hitl/options/{case_id}")
    def hitl_options(task_id: str, case_id: str, _p=Depends(principal)):
        """Gets the available options for an HITL case."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        options = rt.human_queue.options_for(case_id)
        if not options:
            raise HTTPException(404, f"case not found: {case_id}")
        return {"options": [o.model_dump(mode="json") for o in options]}

    # Fase 3: MCP Gateway
    @app.post("/v1/tasks/{task_id}/mcp/invoke")
    def mcp_invoke(task_id: str, b: MCPInvokeBody, _p=Depends(principal)):
        """Invokes a tool through the MCP gateway (§3).

        Routed through the Intermediary like any other tool call:
        authorization, policy, TOOL_INVOKED/TOOL_RETURNED events,
        idempotency, budget and loop detection all apply.
        """
        run = _run_or_404(task_id)
        _require_owner(_p, run)

        ctx = CallContext(
            agent_id=_p.agent_id if _p else run.agent_id,
            instance_id=_p.instance_id if _p else None,
            credential_id=_p.credential_id if _p else None,
            task_id=run.task_id,
            execution_id=run.execution_id,
            stream=run.stream,
            budget=run.budget,
        )

        try:
            res = rt.intermediary.call(ctx, f"{b.system}.{b.tool}", b.args)
        except AuthorizationError as e:
            raise HTTPException(403, str(e))
        except UnknownToolError as e:
            raise HTTPException(404, f"MCP system or tool not found: {e}")
        except AmbiguousInvocationError as e:
            raise HTTPException(409, str(e))
        except IndeterminateOutcome as e:
            raise HTTPException(502, f"indeterminate outcome: {e}")
        except ExecutionLimitError as e:
            raise HTTPException(429, str(e))
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(422, str(e))
        return {"ok": res.ok, "data": res.data, "error": res.error}

    @app.get("/v1/tasks/{task_id}/mcp/tools")
    def mcp_list_tools(task_id: str, system: str | None = None, _p=Depends(principal)):
        """Lists tools available on MCP systems (§3)."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)

        tools = {}
        for name, (adapter, spec) in rt.intermediary._by_tool.items():
            if system and not name.startswith(system + "."):
                continue
            tools[name] = spec.model_dump()

        return {"tools": tools}

    # Fase 4: Policy Engine
    @app.get("/v1/tasks/{task_id}/policy/rules")
    def policy_list_rules(task_id: str, _p=Depends(principal)):
        """Lists all active policy rules (§4)."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        rules = rt.intermediary._policy.list_rules()
        return {
            "rules": [
                {
                    "id": r.id,
                    "name": r.name,
                    "description": r.description,
                    "effect": r.effect.value,
                    "conditions": r.conditions,
                    "priority": r.priority,
                }
                for r in rules
            ]
        }

    @app.post("/v1/tasks/{task_id}/policy/rules")
    def policy_add_rule(task_id: str, b: PolicyRuleBody, _p=Depends(principal)):
        """Adds a new policy rule (§4)."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        try:
            effect = PolicyDecision(b.effect)
        except ValueError:
            raise HTTPException(
                422,
                f"invalid effect: {b.effect}. Must be: allow, deny, require_approval",
            )

        rule = PolicyRule(
            id=b.id,
            name=b.name,
            description=b.description,
            effect=effect,
            conditions=b.conditions,
            priority=b.priority,
        )
        rt.intermediary._policy.add_rule(rule)
        return {"ok": True, "rule_id": rule.id}

    @app.delete("/v1/tasks/{task_id}/policy/rules/{rule_id}")
    def policy_remove_rule(task_id: str, rule_id: str, _p=Depends(principal)):
        """Removes a policy rule (§4)."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        removed = rt.intermediary._policy.remove_rule(rule_id)
        if not removed:
            raise HTTPException(404, f"rule not found: {rule_id}")
        return {"ok": True}

    @app.post("/v1/tasks/{task_id}/policy/evaluate")
    def policy_evaluate(task_id: str, b: dict, _p=Depends(principal)):
        """Evaluates a tool against the policies without invoking it (§4)."""
        run = _run_or_404(task_id)
        _require_owner(_p, run)

        tool = b.get("tool")
        args = b.get("args", {})
        agent_id = b.get("agent_id", _p.agent_id if _p else "unknown")

        if not tool:
            raise HTTPException(422, "'tool' field required")

        # Look up the tool spec
        spec = None
        for name, (adapter, s) in rt.intermediary._by_tool.items():
            if name == tool or name.endswith("." + tool):
                spec = s
                break

        from vexinter.core.policy import PolicyContext

        policy_ctx = PolicyContext(
            agent_id=agent_id,
            tool=tool,
            args=args,
            task_id=run.task_id,
            execution_id=run.execution_id,
            metadata={
                "operation_class": spec.operation_class.value if spec else "read_only"
            },
        )
        decision = rt.intermediary._policy.evaluate(policy_ctx)
        return {"decision": decision.value, "tool": tool}

    @app.post("/v1/tasks/{task_id}/redteam/run")
    def redteam_run(task_id: str, b: RedTeamRunBody, _op=Depends(require_admin)):
        """Runs a red team scenario (§5)."""
        run = _run_or_404(task_id)
        from vexinter.core.redteam import RedTeamScenario

        try:
            sc = RedTeamScenario(b.scenario)
        except ValueError:
            raise HTTPException(422, f"invalid scenario: {b.scenario}")

        try:
            finding = rt.redteam.run_scenario(
                sc,
                system=b.system,
                target_tool=b.tool,
                test_args=b.args,
                task_id=run.task_id,
                execution_id=run.execution_id,
                agent_id=_op or "admin",
            )
            return {
                "id": finding.id,
                "scenario": finding.scenario.value,
                "severity": finding.severity.value,
                "status": finding.status.value,
                "title": finding.title,
                "description": finding.description,
                "recommendation": finding.recommendation,
                "evidence": finding.evidence,
            }
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(422, str(e))

    @app.post("/v1/tasks/{task_id}/redteam/suite")
    def redteam_suite(task_id: str, b: RedTeamSuiteBody, _op=Depends(require_admin)):
        """Runs a full red team suite (§5)."""
        run = _run_or_404(task_id)
        from vexinter.core.redteam import CRITICAL_REDTEAM_SUITE, STANDARD_REDTEAM_SUITE

        suite = CRITICAL_REDTEAM_SUITE if b.critical_only else STANDARD_REDTEAM_SUITE
        try:
            findings = rt.redteam.run_suite(
                suite,
                target_system=b.system,
                target_tool=b.tool,
                test_args=b.args,
                task_id=run.task_id,
                execution_id=run.execution_id,
                agent_id=_op or "admin",
            )
            return {
                "count": len(findings),
                "findings": [
                    {
                        "id": f.id,
                        "scenario": f.scenario.value,
                        "severity": f.severity.value,
                        "status": f.status.value,
                        "title": f.title,
                    }
                    for f in findings
                ],
            }
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(422, str(e))

    @app.get("/v1/tasks/{task_id}/redteam/report")
    def redteam_report(task_id: str, _op=Depends(require_admin)):
        """Gets the consolidated red team report (§5)."""
        _run_or_404(task_id)
        return rt.redteam.generate_report()

    @app.post("/v1/tasks/{task_id}/plan")
    def run_plan(task_id: str, b: PlanBody, _p=Depends(principal)):
        from vexinter.core.coordinator import Plan

        run = _run_or_404(task_id)
        _require_owner(_p, run)
        try:
            v = rt.coordinator.run(Plan(**b.plan), run)
        except AuthorizationError as e:
            raise HTTPException(403, str(e))
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(422, f"plan failed: {e}")
        if v is not None:
            rt.last_verdicts[run.execution_id] = v
            if v.status == "inconclusive" or v.followup_required:
                rt.scheduler.schedule(
                    run,
                    rt.followup_delay_s,
                    "inconclusive or provisional result (§4.4)",
                )
        return {"verdict": v.model_dump(mode="json") if v else None}

    @app.post("/v1/tasks/{task_id}/complete")
    def complete_task(task_id: str, b: CompleteBody, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        if run.completed:
            raise HTTPException(409, "execution already completed")
        rt.store.append(
            EventDraft(
                type=EventType.EXECUTION_COMPLETED,
                task_id=run.task_id,
                execution_id=run.execution_id,
                payload={"exception": b.exception, "last_status": b.last_status},
            ),
            stream=run.stream,
        )
        run.completed = True
        v = rt.last_verdicts.get(run.execution_id)
        if v is not None:
            rt.memory.record_episode(run, v)
        else:
            target = (
                ExecutionState.INCONCLUSIVE
                if b.exception
                else ExecutionState.PENDING_VERIFICATION
            )
            if rt.tracker.can(run.execution_id, target):
                rt.tracker.transition(
                    run, target, "execution finished without verification"
                )
        return {"completed": True}

    @app.post("/v1/tasks/{task_id}/collaborators")
    def add_collaborator(task_id: str, b: CollaboratorBody, _p=Depends(principal)):
        """The task OWNER authorizes another agent to invoke tools
        on it (multi-agent flows, §10)."""
        from vexinter.identity import IdentityError

        run = _run_or_404(task_id)
        _require_owner(_p, run)
        if not dev_mode:
            try:
                rt.identity.identity(b.agent)
            except IdentityError:
                raise HTTPException(422, f"unknown agent: {b.agent}")
        if b.agent != run.agent_id and b.agent not in run.collaborators:
            rt.store.append(
                EventDraft(
                    type=EventType.TASK_ASSIGNED,
                    task_id=run.task_id,
                    execution_id=run.execution_id,
                    agent_id=run.agent_id,
                    payload={"collaborator": b.agent},
                ),
                stream=run.stream,
            )
            run.collaborators.append(b.agent)
        return {"collaborators": sorted(run.collaborators)}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        return {
            "task_id": run.task_id,
            "execution_id": run.execution_id,
            "agent": run.agent_id,
            "objective": run.objective,
            "state": rt.tracker.current(run.execution_id).value,
        }

    @app.get("/v1/tasks/{task_id}/events")
    def task_events(task_id: str, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        return [
            e.model_dump(mode="json")
            for e in rt.store.query(EventQuery(task_id=task_id, limit=10_000))
        ]

    @app.get("/v1/tasks/{task_id}/graph")
    def task_graph(task_id: str, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        return execution_graph(
            rt.store.query(EventQuery(task_id=task_id, limit=10_000))
        )

    @app.get("/v1/tasks/{task_id}/audit")
    def task_audit(task_id: str, _p=Depends(principal)):
        run = _run_or_404(task_id)
        _require_owner(_p, run)
        return rt.store.verify_chain(run.stream).model_dump()

    @app.post("/v1/streams/{stream}/events", dependencies=[Depends(require_admin)])
    def append_event(stream: str, b: EventBody):
        try:
            et = EventType(b.type)
        except ValueError:
            raise HTTPException(422, f"unknown type: {b.type}")
        ev = rt.store.append(
            EventDraft(
                type=et,
                payload=b.payload,
                task_id=b.task_id,
                execution_id=b.execution_id,
                agent_id=b.agent_id,
                operation_id=b.operation_id,
                caused_by=b.caused_by,
                depends_on=b.depends_on,
            ),
            stream=stream,
        )
        return {"id": ev.id, "seq": ev.seq, "this_hash": ev.this_hash}

    @app.post("/v1/privacy/erase")
    def privacy_erase(b: EraseBody, operador=Depends(require_admin)):
        """Cryptographic erasure per execution (§16): destroys the key that
        encrypts its personal content. Only finished executions with no
        pending cases."""
        if not hasattr(rt.store, "erase"):
            raise HTTPException(
                501, "per-execution encryption is not enabled (VEXINTER_KEYRING_DIR)"
            )
        por_exec = {r.execution_id: r for r in rt.runs.values()}
        pendientes = {c.task_id for c in rt.human_queue.pending()}
        borradas, rechazadas = [], {}
        for eid in b.execution_ids:
            run = por_exec.get(eid)
            if run is not None:
                if run.task_id in pendientes:
                    rechazadas[eid] = "has a pending decision case"
                    continue
                if rt.tracker.current(eid) not in (
                    ExecutionState.SATISFIED,
                    ExecutionState.FAILED,
                    ExecutionState.INCONCLUSIVE,
                ):
                    rechazadas[eid] = "execution has not finished"
                    continue
            rt.store.erase(eid, reason=b.reason, requested_by=operador or "")
            if run is not None:
                rt.runs.pop(run.task_id, None)
                rt.last_verdicts.pop(eid, None)
                rt.memory.forget_tasks([run.task_id])
            borradas.append(eid)
        if rechazadas and not borradas:
            raise HTTPException(409, rechazadas)
        return {"erased": borradas, "rejected": rechazadas}

    # === GDPR Art.17 per-person erasure ===
    # The person index and erasure requests are event-sourced: they
    # rebuild from the log on restart, so they work across workers.
    from vexinter.core.gdpr_erasure import (
        GDPRErasureService,
        PersonIndex,
        auto_index_person_from_events,
    )

    _gdpr_index = PersonIndex(
        store=rt.store,
        index_path=os.environ.get("VEXINTER_GDPR_INDEX_PATH") or None)
    _gdpr = GDPRErasureService(
        rt.store, _gdpr_index,
        erasure_log_path=os.environ.get("VEXINTER_GDPR_LOG_PATH") or None,
        eraser=rt.store.erase if hasattr(rt.store, "erase") else None)

    @app.post("/v1/gdpr/index", dependencies=[Depends(require_admin)])
    def gdpr_index_backfill():
        """Backfills the person index from existing log events."""
        return {"indexed": auto_index_person_from_events(rt.store,
                                                         _gdpr_index)}

    @app.get("/v1/gdpr/persons/{person_id}",
             dependencies=[Depends(require_admin)])
    def gdpr_person(person_id: str):
        entry = _gdpr_index.get_person_data(person_id)
        if entry is None:
            raise HTTPException(404, "unknown person")
        return {
            "person_id": entry.person_id,
            "execution_ids": sorted(entry.execution_ids),
            "task_ids": sorted(entry.task_ids),
            "agent_ids": sorted(entry.agent_ids),
            "credential_ids": sorted(entry.credential_ids),
        }

    @app.post("/v1/gdpr/erasure-requests",
              dependencies=[Depends(require_admin)])
    def gdpr_request_erasure(b: GdprErasureBody):
        """Creates an erasure request and returns it WITH the
        verification token — deliver the token to the data subject
        out-of-band, then confirm via /verify."""
        req = _gdpr.request_erasure(
            b.person_id, requested_by=b.requested_by, reason=b.reason)
        return {"request": req.model_dump(mode="json"),
                "verification_token": req.verification_token}

    @app.post("/v1/gdpr/erasure-requests/{request_id}/verify",
              dependencies=[Depends(require_admin)])
    def gdpr_verify_erasure(request_id: str, b: GdprVerifyBody):
        try:
            req = _gdpr.verify_and_execute(request_id, b.verification_token)
        except ValueError as e:
            raise HTTPException(404 if "not found" in str(e).lower()
                                else 403, str(e))
        return {"request": req.model_dump(mode="json")}

    @app.get("/v1/gdpr/erasure-requests",
             dependencies=[Depends(require_admin)])
    def gdpr_list_requests(person_id: str | None = None):
        return {"requests": [
            r.model_dump(mode="json")
            for r in _gdpr.list_requests(person_id)]}

    @app.get("/v1/events")
    def query_events(
        task_id: str | None = None,
        execution_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 1000,
        _p=Depends(principal),
    ):
        if _p is not None:
            if agent_id is not None and agent_id != _p.agent_id:
                raise HTTPException(
                    403, "can only query own events"
                )
            if task_id is not None:
                _require_owner(_p, _run_or_404(task_id))
        evs = rt.store.query(
            EventQuery(
                task_id=task_id,
                execution_id=execution_id,
                agent_id=agent_id,
                limit=limit,
            )
        )
        if _p is not None:
            propias = {t for t, r in rt.runs.items() if r.agent_id == _p.agent_id}
            if execution_id is not None and task_id is None:
                # No own run uses this execution_id: the post-filter would
                # return [] anyway — short-circuit so the query timing does
                # not reveal whether the execution exists at all.
                propias_exec = {
                    getattr(r, "execution_id", None)
                    for r in rt.runs.values() if r.agent_id == _p.agent_id
                }
                if execution_id not in propias_exec:
                    return []
            evs = [e for e in evs if e.agent_id == _p.agent_id or e.task_id in propias]
        return [e.model_dump(mode="json") for e in evs]

    @app.post("/v1/messages")
    def send_message(b: EnvelopeBody, _p=Depends(principal)):
        from vexinter.identity import Envelope

        try:
            env = Envelope(**b.envelope)
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            raise HTTPException(422, f"invalid envelope: {e}")
        if _p is not None and env.header.from_agent != _p.agent_id:
            raise HTTPException(403, "envelope does not match the credential")
        try:
            rt.messaging.send(env)
        except Rejection as r:
            raise HTTPException(422, {"reason": r.reason.value, "detail": r.detail})
        return {"accepted": True, "message_id": env.header.message_id}

    @app.get("/v1/messages/inbox")
    def inbox(agent: str | None = None, _p=Depends(principal)):
        target = _p.agent_id if _p is not None else agent
        if target is None:
            raise HTTPException(400, "agent or credential required")
        return [e.model_dump(mode="json") for e in rt.messaging.pending_for(target)]

    @app.post("/v1/messages/ack")
    def ack_message(b: AckBody, _p=Depends(principal)):
        ev = rt.messaging.acknowledge(
            b.message_id, b.body_hash, _p.agent_id if _p else None
        )
        if ev is None:
            raise HTTPException(422, "invalid message_id or body_hash")
        return {"received": True}

    @app.get("/v1/messages/sequence")
    def msg_sequence(from_agent: str, to_agent: str, _p=Depends(principal)):
        return {"next": rt.messaging.next_sequence(from_agent, to_agent)}

    @app.get("/v1/identity/key")
    def enc_key(agent: str, _p=Depends(principal)):
        try:
            return {"encryption_pub": rt.identity.encryption_pub_for_agent(agent)}
        except Exception:  # noqa: BLE001 - error boundary: converted into result/log
            raise HTTPException(404, f"no active credential for {agent}")

    @app.get("/v1/identity/credentials/{cid}/public")
    def credential_public(cid: str, _p=Depends(principal)):
        """PUBLIC keys of a credential, so the recipient can verify
        an envelope's signature on their own (§9.5)."""
        try:
            c = rt.identity.get_credential(cid)
        except Exception:  # noqa: BLE001 - error boundary: converted into result/log
            raise HTTPException(404, f"unknown credential: {cid}")
        return c.model_dump(mode="json", exclude={"revoked_reason"})

    @app.post("/v1/identity/credentials/rotate")
    def rotate_own_credential(b: RotateBody, _p=Depends(principal)):
        """The agent rotates its own keys by signing with its current
        credential; the previous one stays valid during the grace period."""
        from vexinter.identity import IdentityError

        if _p is None:
            raise HTTPException(400, "rotation requires a credential")
        try:
            nueva = rt.identity.rotate_credential(
                _p.credential_id, b.signing_pub, b.encryption_pub
            )
        except IdentityError as e:
            raise HTTPException(422, str(e))
        anterior = rt.identity.get_credential(_p.credential_id)
        return {
            "credential_id": nueva.credential_id,
            "agent_id": nueva.agent_id,
            "instance_id": nueva.instance_id,
            "expires_at": nueva.expires_at.isoformat(),
            "previous_valid_until": anterior.valid_until.isoformat(),
        }

    @app.get("/v1/memory")
    def memory(
        system: str | None = None,
        agent: str | None = None,
        text: str | None = None,
        _p=Depends(principal),
    ):
        if _p is not None and agent is not None and agent != _p.agent_id:
            raise HTTPException(403, "can only query own memory")
        items = rt.memory.retrieve(
            system=system,
            agent=agent,
            text=text,
            as_agent=_p.agent_id if _p is not None else None,
        )
        return {
            "results": [
                it if isinstance(it, dict) else it.model_dump(mode="json")
                for it in items
            ]
        }

    # Fase 6: Advanced Memory & Learning
    @app.get("/v1/memory/lessons")
    def memory_lessons(
        tags: str | None = None,
        min_confidence: float | None = None,
        _p=Depends(principal),
    ):
        """Gets lessons extracted from memory (§6)."""
        import json

        if tags is not None and len(tags) > 4096:
            raise HTTPException(422, "tags too large")
        try:
            parsed_tags = json.loads(tags) if tags else None
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"invalid tags JSON: {e}") from e
        if parsed_tags is not None and not isinstance(parsed_tags, list):
            raise HTTPException(422, "tags must be a JSON array")
        lessons = rt.memory.get_lessons(parsed_tags, min_confidence)
        return {
            "lessons": [
                {
                    "id": l.id,
                    "pattern": l.pattern,
                    "description": l.description,
                    "confidence": l.confidence,
                    "supporting_episodes": l.supporting_episodes,
                    "tags": l.tags,
                    "created_at": l.created_at,
                    "application_count": l.application_count,
                    "success_count": l.success_count,
                }
                for l in lessons
            ]
        }

    @app.post("/v1/memory/lessons/{lesson_id}/apply")
    def memory_apply_lesson(
        lesson_id: str, b: dict, request: Request,
        x_vex_admin: str | None = Header(None),
        _p=Depends(principal),
    ):
        """Records application of a lesson (§6).

        Lessons are shared/global: any agent may report success, but a
        failure report degrades everyone's confidence — it requires an
        admin token so one agent cannot sabotage another's learning.
        """
        success = bool(b.get("success", False))
        if not success:
            require_admin(request, x_vex_admin)
        rt.memory.apply_lesson(lesson_id, success)
        return {"ok": True}

    @app.post("/v1/memory/search")
    def memory_search(b: MemorySearchBody, _p=Depends(principal)):
        """Semantic similarity search in memory (§6)."""
        results = rt.memory.search_similar(b.query, b.top_k, b.min_score)
        return {"results": results}

    @app.get("/v1/memory/patterns")
    def memory_patterns(_p=Depends(principal)):
        """Gets cross-task patterns (§6)."""
        return rt.memory.get_cross_task_patterns()

    @app.post("/v1/memory/cleanup")
    def memory_cleanup(_p=Depends(principal)):
        """Cleans up expired memories (§6)."""
        rt.memory.cleanup_expired()
        return {"ok": True}

    # Fase 7: Multi-agent Orchestration
    def _resolve_parent(task_id: str):
        """A delegation parent may be a task id OR a subtask id (cascade
        delegation). Returns (run, subtask) — exactly one is not None."""
        run = rt.runs.get(task_id)
        if run is not None:
            return run, None
        return None, rt.subtasks.get_subtask(task_id)

    def _parent_authorized(_p, run, sub) -> bool:
        if _p is None:
            return True
        if run is not None:
            return (_p.agent_id == run.agent_id
                    or _p.agent_id in run.collaborators)
        root = rt.runs.get(sub.root_task_id or sub.parent_task_id)
        allowed = {sub.agent_id}
        if root is not None:
            allowed.add(root.agent_id)
            allowed.update(root.collaborators)
        return _p.agent_id in allowed

    @app.post("/v1/tasks/{task_id}/subtasks")
    def delegate_subtask(task_id: str, b: SubTaskDelegateBody, _p=Depends(principal)):
        """Delegates a subtask to another agent (§7).

        Subtasks do NOT consume task quota: they are work units inside
        the parent task's quota. They inherit the parent's budget and
        are bounded by VEXINTER_MAX_SUBTASK_DEPTH and
        VEXINTER_MAX_SUBTASKS_PER_TASK."""
        run, parent_sub = _resolve_parent(task_id)
        if run is None and parent_sub is None:
            raise HTTPException(404, "task not found")
        if not _parent_authorized(_p, run, parent_sub):
            raise HTTPException(403, "not authorized to delegate on this task")
        parent_agent = run.agent_id if run is not None else parent_sub.agent_id
        parent_exec = (run.execution_id if run is not None
                       else (parent_sub.execution_id
                             or parent_sub.parent_execution_id))
        try:
            subtask = rt.subtasks.delegate_task(
                parent_task_id=task_id,
                parent_execution_id=parent_exec,
                agent_id=b.agent_id,
                objective=b.objective,
                condition=b.condition,
                input=b.input or {},
                parent_agent_id=parent_agent,
                budget=run.budget if run is not None else None,
            )
        except SubTaskLimitError as e:
            raise HTTPException(429, str(e))
        return {"subtask_id": subtask.id, "depth": subtask.depth}

    @app.get("/v1/tasks/{task_id}/subtasks")
    def list_subtasks(task_id: str, _p=Depends(principal)):
        """Lists a task's subtasks (§7)."""
        run, parent_sub = _resolve_parent(task_id)
        if run is None and parent_sub is None:
            raise HTTPException(404, "task not found")
        if not _parent_authorized(_p, run, parent_sub):
            raise HTTPException(403, "not authorized")
        subtasks = rt.subtasks.get_subtasks_for_task(task_id)
        return {
            "subtasks": [
                {
                    "id": s.id,
                    "agent_id": s.agent_id,
                    "objective": s.objective,
                    "status": s.status.value,
                    "depth": s.depth,
                    "created_at": s.created_at,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "result": s.result,
                    "error": s.error,
                }
                for s in subtasks
            ]
        }

    @app.get("/v1/tasks/{task_id}/subtasks/{subtask_id}")
    def get_subtask(task_id: str, subtask_id: str, _p=Depends(principal)):
        """Gets a subtask (§7)."""
        run, parent_sub = _resolve_parent(task_id)
        if run is None and parent_sub is None:
            raise HTTPException(404, "task not found")
        if not _parent_authorized(_p, run, parent_sub):
            raise HTTPException(403, "not authorized")
        s = rt.subtasks.get_subtask(subtask_id)
        if not s:
            raise HTTPException(404, "subtask not found")
        return {
            "id": s.id,
            "agent_id": s.agent_id,
            "objective": s.objective,
            "condition": s.condition,
            "input": s.input,
            "status": s.status.value,
            "depth": s.depth,
            "root_task_id": s.root_task_id,
            "budget": (s.budget.model_dump()
                       if hasattr(s.budget, "model_dump") else s.budget),
            "created_at": s.created_at,
            "started_at": s.started_at,
            "completed_at": s.completed_at,
            "result": s.result,
            "error": s.error,
        }

    @app.post("/v1/tasks/{task_id}/messages")
    def send_agent_message(task_id: str, b: AgentMessageBody, _p=Depends(principal)):
        """Sends a message to another agent on the task (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        msg = rt.agent_messaging.send_message(
            task_id=task_id,
            from_agent=run.agent_id,
            to_agent=b.to_agent,
            content=b.content,
            message_type=b.message_type,
        )
        return {"message_id": msg.id}

    @app.get("/v1/tasks/{task_id}/messages")
    def get_agent_messages(
        task_id: str, unread_only: bool = False, _p=Depends(principal)
    ):
        """Gets the current agent's messages on the task (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        messages = rt.agent_messaging.get_messages_for_task(run.agent_id, task_id)
        if unread_only:
            messages = [m for m in messages if m.read_at is None]
        return {
            "messages": [
                {
                    "id": m.id,
                    "task_id": m.task_id,
                    "from_agent": m.from_agent,
                    "content": m.content,
                    "message_type": m.message_type,
                    "created_at": m.created_at,
                    "read_at": m.read_at,
                }
                for m in messages
            ]
        }

    @app.post("/v1/tasks/{task_id}/messages/{message_id}/read")
    def mark_message_read(task_id: str, message_id: str, _p=Depends(principal)):
        """Marks a message as read (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        ok = rt.agent_messaging.mark_read(run.agent_id, message_id)
        return {"ok": ok}

    @app.post("/v1/tasks/{task_id}/delegation/sequential")
    def run_sequential_delegation(
        task_id: str, b: SequentialDelegationBody, _p=Depends(principal)
    ):
        """Runs sequential delegation (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        try:
            subtasks = rt.orchestrator.run_sequential(
                parent_task_id=task_id,
                parent_execution_id=run.execution_id,
                parent_agent_id=run.agent_id,
                steps=b.steps,
                budget=run.budget,
            )
        except SubTaskLimitError as e:
            raise HTTPException(429, str(e))
        return {"subtask_ids": [s.id for s in subtasks]}

    @app.post("/v1/tasks/{task_id}/delegation/parallel")
    def run_parallel_delegation(
        task_id: str, b: ParallelDelegationBody, _p=Depends(principal)
    ):
        """Runs parallel delegation (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        try:
            subtasks = rt.orchestrator.run_parallel(
                parent_task_id=task_id,
                parent_execution_id=run.execution_id,
                parent_agent_id=run.agent_id,
                steps=b.steps,
                budget=run.budget,
            )
        except SubTaskLimitError as e:
            raise HTTPException(429, str(e))
        return {"subtask_ids": [s.id for s in subtasks]}

    @app.get("/v1/tasks/{task_id}/delegation/aggregate")
    def aggregate_subtask_results(task_id: str, _p=Depends(principal)):
        """Aggregates subtask results (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        return rt.orchestrator.aggregate_results(task_id)

    @app.post("/v1/tasks/{task_id}/notify")
    def notify_agents(task_id: str, b: NotifyAgentsBody, _p=Depends(principal)):
        """Notifies multiple agents (§7)."""
        run = rt.runs.get(task_id)
        if not run:
            raise HTTPException(404, "task not found")
        if (
            _p is not None
            and _p.agent_id != run.agent_id
            and _p.agent_id not in run.collaborators
        ):
            raise HTTPException(403, "not authorized")
        messages = rt.orchestrator.notify_agents(
            task_id=task_id,
            from_agent=run.agent_id,
            to_agents=b.to_agents,
            content=b.content,
            message_type=b.message_type,
        )
        return {"message_ids": [m.id for m in messages]}

    from vexinter.identity import IdentityError

    @app.post("/v1/identity/agents", dependencies=[Depends(require_admin)])
    def enroll(b: EnrollBody):
        if not dev_mode and b.allowed_tools is None:
            raise HTTPException(
                422,
                "in authenticated mode allowed_tools must be declared "
                "(an empty list means no tools)",
            )
        try:
            a = rt.identity.enroll_agent(
                b.agent_id,
                display_name=b.display_name,
                allowed_tools=(
                    set(b.allowed_tools) if b.allowed_tools is not None else None
                ),
                allowed_recipients=(
                    set(b.allowed_recipients)
                    if b.allowed_recipients is not None
                    else None
                ),
            )
        except IdentityError as e:
            raise HTTPException(422, str(e))
        return a.model_dump(mode="json")

    @app.get("/v1/identity/agents", dependencies=[Depends(require_admin)])
    def list_agents():
        return [a.model_dump(mode="json") for a in rt.identity.list_agents()]

    # === Tenants (multi-tenancy, §11) ===

    @app.post("/v1/admin/tenants", dependencies=[Depends(require_admin)])
    def create_tenant(b: TenantBody):
        from vexinter.core.multitenancy import TenantTier

        try:
            tier = TenantTier(b.tier)
        except ValueError:
            raise HTTPException(422, f"invalid tier: {b.tier}")
        t = tenant_svc.create_tenant(b.name, tier=tier,
                                     owner_user_id=b.owner_user_id)
        return {"tenant_id": t.tenant_id, "name": t.name, "tier": t.tier.value,
                "status": t.status.value}

    @app.get("/v1/admin/tenants", dependencies=[Depends(require_admin)])
    def list_tenants():
        return [
            {"tenant_id": t.tenant_id, "name": t.name, "tier": t.tier.value,
             "status": t.status.value, "is_active": t.is_active,
             "agents": len(tenant_svc.agents_of_tenant(t.tenant_id)),
             "executions_today": tenant_svc.executions_today(t.tenant_id)}
            for t in tenant_svc._tenants.values()
        ]

    @app.get("/v1/admin/tenants/{tenant_id}", dependencies=[Depends(require_admin)])
    def get_tenant(tenant_id: str):
        t = tenant_svc.get_tenant(tenant_id)
        if t is None:
            raise HTTPException(404, "tenant not found")
        return {"tenant_id": t.tenant_id, "name": t.name, "tier": t.tier.value,
                "status": t.status.value, "is_active": t.is_active,
                "quotas": t.quotas, "agents": tenant_svc.agents_of_tenant(tenant_id),
                "executions_today": tenant_svc.executions_today(tenant_id)}

    @app.post("/v1/admin/tenants/{tenant_id}/suspend", dependencies=[Depends(require_admin)])
    def suspend_tenant(tenant_id: str):
        if not tenant_svc.suspend_tenant(tenant_id):
            raise HTTPException(404, "tenant not found")
        return {"tenant_id": tenant_id, "status": "suspended"}

    @app.post("/v1/admin/tenants/{tenant_id}/activate", dependencies=[Depends(require_admin)])
    def activate_tenant(tenant_id: str):
        if not tenant_svc.activate_tenant(tenant_id):
            raise HTTPException(404, "tenant not found")
        return {"tenant_id": tenant_id, "status": "active"}

    @app.post("/v1/admin/tenants/{tenant_id}/members", dependencies=[Depends(require_admin)])
    def add_tenant_member(tenant_id: str, b: TenantMemberBody):
        from vexinter.core.multitenancy import TenantRole

        try:
            roles = {TenantRole(r) for r in b.roles}
        except ValueError as e:
            raise HTTPException(422, f"invalid role: {e}")
        try:
            m = tenant_svc.add_member(tenant_id, b.user_id, roles)
        except ValueError as e:
            raise HTTPException(429, str(e))
        if m is None:
            raise HTTPException(404, "tenant not found")
        return {"member_id": m.member_id, "user_id": m.user_id,
                "roles": [r.value for r in m.roles]}

    @app.get("/v1/admin/tenants/{tenant_id}/members", dependencies=[Depends(require_admin)])
    def list_tenant_members(tenant_id: str):
        if tenant_svc.get_tenant(tenant_id) is None:
            raise HTTPException(404, "tenant not found")
        return [
            {"member_id": m.member_id, "user_id": m.user_id,
             "roles": [r.value for r in m.roles], "is_active": m.is_active}
            for m in tenant_svc.list_members(tenant_id)
        ]

    @app.post("/v1/admin/tenants/{tenant_id}/agents", dependencies=[Depends(require_admin)])
    def assign_tenant_agent(tenant_id: str, b: TenantAgentBody):
        if tenant_svc.get_tenant(tenant_id) is None:
            raise HTTPException(404, "tenant not found")
        try:
            ok = tenant_svc.assign_agent(tenant_id, b.agent_id)
        except ValueError as e:
            raise HTTPException(429, str(e))
        if not ok:
            raise HTTPException(404, "tenant not found")
        return {"tenant_id": tenant_id, "agent_id": b.agent_id}

    @app.delete("/v1/admin/tenants/{tenant_id}/agents/{agent_id}",
                dependencies=[Depends(require_admin)])
    def unassign_tenant_agent(tenant_id: str, agent_id: str):
        if not tenant_svc.unassign_agent(tenant_id, agent_id):
            raise HTTPException(404, "binding not found")
        return {"tenant_id": tenant_id, "agent_id": agent_id}

    @app.post("/v1/identity/instances", dependencies=[Depends(require_admin)])
    def spawn(b: InstanceBody):
        # License binding: a paid plan caps concurrently active
        # instances. Tenant-bound agents are exempt — they carry their
        # own tenant quotas on multi-tenant deployments.
        cap = _license_max_instances()
        if cap is not None:
            active = [
                i for i in rt.identity.list_instances()
                if getattr(i, "status", "active") != "revoked"
                and tenant_svc.tenant_of_agent(i.agent_id) is None
            ]
            if len(active) >= cap:
                raise HTTPException(
                    403,
                    f"license allows at most {cap} active agent "
                    f"instances ({len(active)} in use); revoke an "
                    "instance or upgrade the plan")
        try:
            return rt.identity.spawn_instance(b.agent_id).model_dump(mode="json")
        except IdentityError as e:
            raise HTTPException(422, str(e))

    @app.post("/v1/identity/credentials", dependencies=[Depends(require_admin)])
    def issue(b: CredIssueBody):
        try:
            return rt.identity.issue_credential(
                b.instance_id, b.signing_pub, b.encryption_pub
            ).model_dump(mode="json")
        except IdentityError as e:
            raise HTTPException(422, str(e))

    @app.delete("/v1/identity/credentials/{cid}", dependencies=[Depends(require_admin)])
    def revoke_cred(cid: str, reason: str = ""):
        try:
            rt.identity.revoke_credential(cid, reason or "administrative revocation")
        except IdentityError as e:
            raise HTTPException(404, str(e))
        return {"revoked": cid}

    @app.post(
        "/v1/identity/instances/{iid}/revoke", dependencies=[Depends(require_admin)]
    )
    def revoke_inst(iid: str, b: RevokeBody):
        try:
            rt.identity.revoke_instance(iid, b.reason or "administrative revocation")
        except IdentityError as e:
            raise HTTPException(404, str(e))
        return {"revoked": iid}

    @app.get("/v1/cases", dependencies=[Depends(require_admin)])
    def cases():
        return [
            {
                "case": c.model_dump(mode="json"),
                "options": [
                    o.model_dump(mode="json") for o in rt.human_queue.options(c.id)
                ],
            }
            for c in rt.human_queue.pending()
        ]

    @app.post("/v1/cases/{case_id}/decide")
    def decide(case_id: str, b: DecideBody, operador=Depends(require_admin)):
        try:
            v = rt.human_queue.decide(case_id, b.action, operador or b.operator)
        except StateError as e:
            raise HTTPException(409, str(e))
        except KeyError:
            raise HTTPException(404, f"unknown case: {case_id}")
        except LookupError as e:
            raise HTTPException(409, str(e))
        except AuthorizationError as e:
            raise HTTPException(403, str(e))
        return v.model_dump(mode="json") if v else {"decided": True}

    @app.post("/v1/followups/run-due", dependencies=[Depends(require_admin)])
    def run_due():
        return [
            {"status": v.status, "task_id": v.task_id} for v in rt.scheduler.run_due()
        ]

    @app.post("/v1/checkpoints", dependencies=[Depends(require_admin)])
    def checkpoint():
        return rt.store.take_checkpoint(checkpoint_signer)

    @app.get("/v1/metrics", dependencies=[Depends(require_admin)])
    def metrics():
        return compute_metrics(rt.store)

    metrics_token = os.environ.get("VEXINTER_METRICS_TOKEN") or None
    if metrics_token is not None and len(metrics_token) < 12:
        raise ValueError(
            "VEXINTER_METRICS_TOKEN is too short (minimum 12 characters)"
        )

    def metrics_access(
        x_vex_admin: str | None = Header(None), authorization: str | None = Header(None)
    ):
        """/metrics: admin or metrics token, via X-Vex-Admin
        or Authorization: Bearer (what Prometheus sends)."""
        if dev_mode:
            return
        dado = x_vex_admin
        if dado is None and authorization and authorization[:7].lower() == "bearer ":
            dado = authorization[7:].strip()
        dado_b = (dado or "").encode()
        # Extract token strings from operators dict (which has structure name -> {token, role, expires_at})
        validos = [meta["token"] for meta in ops_norm.values()]
        if metrics_token:
            validos.append(metrics_token)
        ok = False
        for tok in validos:  # no short-circuit on the first match
            if hmac.compare_digest(dado_b, tok.encode()):
                ok = True
        if not ok:
            raise HTTPException(
                403, "admin or metrics token required"
            )

    @app.get("/metrics", dependencies=[Depends(metrics_access)])
    def prometheus_metrics():
        """Prometheus endpoint (§13.5), with authentication."""
        from fastapi import Response

        m = compute_metrics(rt.store)

        def lbl(valor) -> str:
            # Reasons may carry free text (e.g. a validation
            # error): cut at the first ':' to avoid multiplying
            # series and escape it so the exposition format is not broken.
            t = str(valor).split(":", 1)[0][:80]
            return (
                t.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", " ")
                .replace("\r", " ")
            )

        lines: list[str] = []
        for nombre, clave, ayuda in (
            (
                "vexinter_executions_total",
                "executions",
                "Total executions recorded",
            ),
            (
                "vexinter_false_successes_total",
                "false_successes_detected",
                "False successes detected",
            ),
            (
                "vexinter_discrepancies_total",
                "discrepancies",
                "Discrepancies detected",
            ),
            (
                "vexinter_retries_compensations_total",
                "retries_or_compensations",
                "Retries and compensations",
            ),
            (
                "vexinter_human_decisions_requested_total",
                "human_decisions_requested",
                "Human decisions requested",
            ),
            (
                "vexinter_human_decisions_made_total",
                "human_decisions_made",
                "Human decisions executed",
            ),
            (
                "vexinter_rejected_operations_total",
                "rejected_operations",
                "Rejected operations",
            ),
            (
                "vexinter_authentication_errors_total",
                "authentication_errors",
                "Authentication errors",
            ),
            (
                "vexinter_integrity_failures_total",
                "integrity_failures",
                "Integrity failures",
            ),
        ):
            lines += [
                f"# HELP {nombre} {ayuda}",
                f"# TYPE {nombre} counter",
                f"{nombre} {m.get(clave, 0)}",
            ]
        for nombre, tipo, ayuda, etiqueta, datos in (
            (
                "vexinter_final_states",
                "gauge",
                "Executions by current state",
                "state",
                m.get("final_states", {}),
            ),
            (
                "vexinter_message_rejections",
                "counter",
                "Messages rejected by reason",
                "reason",
                m.get("message_rejections_by_reason", {}),
            ),
            (
                "vexinter_tool_rejections",
                "counter",
                "Invocations rejected by reason",
                "reason",
                m.get("tool_rejections_by_reason", {}),
            ),
        ):
            agregado: dict[str, int] = {}
            for k, n in datos.items():  # no duplicate series
                agregado[lbl(k)] = agregado.get(lbl(k), 0) + n
            lines += [f"# HELP {nombre} {ayuda}", f"# TYPE {nombre} {tipo}"]
            for k, n in sorted(agregado.items()):
                lines.append(f'{nombre}{{{etiqueta}="{k}"}} {n}')
        return Response(
            content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4"
        )

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_spec():
        """Service OpenAPI 3.1 specification."""
        return app.openapi()

    @app.get("/v1/chains/verify", dependencies=[Depends(require_admin)])
    def chains(stream: str | None = None):
        return rt.store.verify_chain(stream).model_dump()

    @app.get("/v1/tasks", dependencies=[Depends(require_admin)])
    def list_tasks(state: str | None = None, limit: int = 500):
        """Execution dashboard (§13.1) as data."""
        out = []
        for e in rt.store.query(
            EventQuery(types={EventType.TASK_CREATED}, limit=limit)
        ):
            st = rt.tracker.current(e.execution_id).value if e.execution_id else None
            if state and st != state:
                continue
            out.append(
                {
                    "task_id": e.task_id,
                    "execution_id": e.execution_id,
                    "agent": e.agent_id,
                    "objective": (e.payload or {}).get("objective", ""),
                    "created_at": e.ts,
                    "state": st,
                }
            )
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return out

    @app.get("/v1/cases/{case_id}", dependencies=[Depends(require_admin)])
    def case_detail_json(case_id: str):
        case = rt.human_queue.get_case(case_id)
        if case is None:
            raise HTTPException(404, f"unknown case: {case_id}")
        evidence = []
        for hid in case.history:
            ev = rt.store.get(hid)
            if ev is not None:
                evidence.append(
                    {
                        "id": ev.id,
                        "type": ev.type.value,
                        "ts": ev.ts,
                        "payload": ev.payload,
                    }
                )
        run = rt.runs.get(case.task_id)
        return {
            "case": case.model_dump(mode="json"),
            "objective": run.objective if run else None,
            "options": [o.model_dump() for o in rt.human_queue.options_for(case_id)],
            "evidence": evidence,
        }

    @app.post("/v1/cases/{case_id}/preview", dependencies=[Depends(require_admin)])
    def case_preview_json(case_id: str):
        from .console import preview_case

        data = preview_case(rt, case_id)
        if data is None:
            raise HTTPException(404, f"unknown case: {case_id}")
        return data

    @app.get("/v1/admin/summary", dependencies=[Depends(require_admin)])
    def admin_summary():
        m = compute_metrics(rt.store)
        return {"metrics": m, "cases": len(rt.human_queue.pending())}

    @app.get("/v1/identity/credentials", dependencies=[Depends(require_admin)])
    def list_credentials(agent: str | None = None):
        return [c.model_dump(mode="json") for c in rt.identity.list_credentials(agent)]

    @app.get("/v1/identity/instances", dependencies=[Depends(require_admin)])
    def list_instances(agent: str | None = None):
        return [i.model_dump(mode="json") for i in rt.identity.list_instances(agent)]

    @app.get("/v1/admin/tasks/{task_id}", dependencies=[Depends(require_admin)])
    def admin_task(task_id: str):
        """Full detail for the console (§13.2): everything the explorer
        page needs, in a single renderable JSON."""
        run = rt.runs.get(task_id)
        if run is None:
            raise HTTPException(404, f"unknown task: {task_id}")
        evs = rt.store.query(EventQuery(task_id=task_id, limit=10_000))
        state = rt.tracker.current(run.execution_id)
        verdicts = [
            {
                "ts": e.ts.isoformat(),
                "status": (e.payload or {}).get("status"),
                "provisional": (e.payload or {}).get("provisional", False),
                "scope": (e.payload or {}).get("scope", "task"),
            }
            for e in evs
            if e.type == EventType.VERIFIER_VERDICT and e.payload
        ]
        events = []
        for e in evs:
            p = e.payload or {}
            label = next(
                (
                    str(p[k])
                    for k in (
                        "tool",
                        "to",
                        "status",
                        "case_id",
                        "strategy",
                        "message_id",
                    )
                    if k in p
                ),
                "",
            )
            rels = []
            if e.caused_by:
                rels.append(f"← {e.caused_by[:10]}…")
            for dep in e.depends_on:
                rels.append(f"⚑ {dep[:10]}…")
            if e.compensates:
                rels.append(f"↩ compensates {e.compensates[:10]}…")
            events.append(
                {
                    "seq": e.seq,
                    "ts": e.ts.isoformat(),
                    "type": e.type.value,
                    "label": label,
                    "rels": " ".join(rels) or "—",
                    "payload": json.dumps(p, ensure_ascii=False, default=str)[:1200],
                }
            )
        return {
            "task_id": task_id,
            "execution_id": run.execution_id,
            "objective": run.objective,
            "agent": run.agent_id,
            "state": state.value,
            "insufficient": (
                state is ExecutionState.INCONCLUSIVE
                or any(v["status"] == "inconclusive" for v in verdicts)
            ),
            "verdicts": verdicts,
            "events": events,
            "graph": execution_graph(evs),
            "audit": rt.store.verify_chain(run.stream).model_dump(),
        }

    if console:
        try:
            from .console import mount_console

            mount_console(app, rt, admin_token, dev_mode, ops_norm)
        except ImportError:
            logging.getLogger("vexinter").warning(
                "jinja2 is not installed: console disabled "
                "(pip install 'vexinter[server]')"
            )


    ui_dist = Path(
        os.environ.get("VEXINTER_UI_DIST")
        or (Path(__file__).resolve().parent.parent / "vexinter" / "app" / "dist")
    )
    if ui_dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        from starlette.exceptions import HTTPException as _HTTPError

        class _SPAStatic(StaticFiles):
            """Serves the UI. A client route (no extension) that is not
            a file returns index.html, so reloading /app/tasks/x
            works. A missing file is still a 404."""

            async def get_response(self, path, scope):
                try:
                    return await super().get_response(path, scope)
                except _HTTPError as e:
                    if e.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                        return await super().get_response("index.html", scope)
                    raise

        app.mount("/app", _SPAStatic(directory=str(ui_dist), html=True))

    # Phase 8: Startup/Shutdown (lifespan en vez de on_event deprecado)
    scheduler_interval = float(
        os.environ.get("VEXINTER_SCHEDULER_INTERVAL_S", "0") or 0)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        log_audit_event("server_start", "system", "server", "startup", "success")
        sched_task = None
        if scheduler_interval > 0:
            async def _scheduler_loop() -> None:
                while True:
                    await asyncio.sleep(scheduler_interval)
                    try:
                        await asyncio.to_thread(
                            rt.scheduler.run_due)
                        await asyncio.to_thread(
                            rt.memory.cleanup_expired)
                    except Exception:
                        logging.getLogger("vexinter").exception(
                            "scheduler tick failed")
            sched_task = asyncio.create_task(_scheduler_loop())
        try:
            yield
        finally:
            if sched_task is not None:
                sched_task.cancel()
            log_audit_event("server_shutdown", "system", "server",
                            "shutdown", "success")
            await rt.shutdown.shutdown()

    app.router.lifespan_context = _lifespan

    return app


def InMemorySequenceGuard_safe(store):
    """Chooses the guard based on the store type, without requiring psycopg."""
    retention = 2 * MAX_SKEW_S + 60
    from vexinter.core.store import InMemorySequenceGuard

    if hasattr(store, "_pool"):
        from vexinter.store_pg import PostgresSequenceGuard

        return PostgresSequenceGuard(store, retention_s=retention)
    return InMemorySequenceGuard(retention_s=retention)


app = make_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
