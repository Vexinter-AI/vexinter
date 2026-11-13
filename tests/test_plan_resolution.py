"""Plan resolution hardening.

A paid plan must come from a verified license document or an explicit
tenant binding — never from env vars alone (dev mode excepted). Tenant-
bound agents resolve to the tenant's tier and get a per-tenant usage
bucket instead of the shared subscriber one.
"""
import asyncio

import httpx
import pytest
from httpx import BaseTransport, ByteStream, Request, Response

from examples.demo_identidad import COND
from services.app import make_app
from vexinter import Vexinter
from vexinter.core.store import InMemoryEventStore
from vexinter.core.tiers import TierManager, get_tier_manager, set_tier_manager
from vexinter.demo import BillingSystem, CrmSystem
from vexinter.identity import AgentKeyMaterial


class SyncASGITransport(BaseTransport):
    def __init__(self, app):
        self._transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: Request) -> Response:
        async def send():
            res = await self._transport.handle_async_request(request)
            body = await res.aread()
            return res.status_code, res.headers.raw, body

        status_code, headers, body = asyncio.run(send())
        return Response(status_code=status_code, headers=headers,
                        stream=ByteStream(body), request=request)


def _transport(app):
    return SyncASGITransport(app=app)


def _app(store=None, **kw):
    store = store or InMemoryEventStore()
    set_tier_manager(TierManager(store=store))
    return make_app(store=store,
                    adapters=[CrmSystem(), BillingSystem()],
                    dev_mode=False, admin_token="tok", **kw)


def _agente(app):
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("a", allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("a")
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    vex = Vexinter(endpoint="http://localhost", credentials=rc,
                   transport=_transport(app))
    return adm, vex


def _usage_keys(store):
    return set(get_tier_manager(store)._usage_cache.keys())


def _no_license_env(monkeypatch):
    for var in ("VEXINTER_LICENSE_BASE_URL", "VEXINTER_LICENSE_ID",
                "VEXINTER_LICENSE_PUBKEY", "VEXINTER_LICENSE_CACHE",
                "VEXINTER_LICENSE_PUBKEYS", "VEXINTER_LICENSE_MAX_STALE_S"):
        monkeypatch.delenv(var, raising=False)


def test_env_plan_cannot_grant_paid_tier_without_license(monkeypatch):
    """VEXINTER_DEFAULT_PLAN=business must NOT unlock Business limits on
    a non-dev deployment with no license configured."""
    _no_license_env(monkeypatch)
    monkeypatch.setenv("VEXINTER_DEFAULT_PLAN", "business")
    store = InMemoryEventStore()
    app = _app(store)
    _, vex = _agente(app)

    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": "x@y.z"}):
        pass

    keys = _usage_keys(store)
    assert keys, "expected a tier-usage bucket to be recorded"
    assert all(k.endswith(":community") for k in keys), keys
    assert any(k.startswith("community@vexinter.com") for k in keys)


def test_dev_mode_env_plan_still_works(monkeypatch):
    """Dev mode keeps the env override for local testing."""
    _no_license_env(monkeypatch)
    monkeypatch.setenv("VEXINTER_DEFAULT_PLAN", "business")
    store = InMemoryEventStore()
    set_tier_manager(TierManager(store=store))
    app = make_app(store=store, adapters=[CrmSystem(), BillingSystem()],
                   dev_mode=True, admin_token="tok")

    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post("/v1/tasks", json={
        "agent": "agente1", "objective": "o", "condition": COND,
        "input": {}})
    assert r.status_code == 200, r.text

    keys = _usage_keys(store)
    assert any(k.endswith(":business") for k in keys), keys


def test_tenant_tier_overrides_and_gets_own_bucket(monkeypatch):
    """An agent bound to a professional tenant resolves to the
    professional plan and charges the tenant's own usage bucket."""
    _no_license_env(monkeypatch)
    monkeypatch.setenv("VEXINTER_DEFAULT_PLAN", "business")
    store = InMemoryEventStore()
    app = _app(store)
    adm, vex = _agente(app)

    raw = httpx.Client(transport=_transport(app),
                       base_url="http://localhost")
    r = raw.post("/v1/admin/tenants",
                 json={"name": "acme", "tier": "professional",
                       "owner_user_id": "u1"},
                 headers={"X-Vex-Admin": "tok"})
    assert r.status_code == 200, r.text
    tid = r.json()["tenant_id"]
    r = raw.post(f"/v1/admin/tenants/{tid}/agents",
                 json={"agent_id": "a"},
                 headers={"X-Vex-Admin": "tok"})
    assert r.status_code == 200, r.text

    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": "x@y.z"}):
        pass

    keys = _usage_keys(store)
    assert f"{tid}:professional" in keys, keys
    assert not any(k.endswith(":business") for k in keys), keys
