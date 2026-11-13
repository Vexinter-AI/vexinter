import asyncio

import httpx
import pytest

from services.app import make_app
from vexinter.core.store import InMemoryEventStore
from vexinter.demo import BillingSystem, CrmSystem
from vexinter.identity import AgentKeyMaterial
from vexinter.remote import AsyncRemoteRuntime, AsyncRemoteTask, RemoteError

ADMIN = "tok-de-administracion"
EMAIL = "ana@example.com"
COND = {"op": "exists", "system": "crm",
        "query": {"type": "customer",
                  "where": {"email": {"$ref": "input.email"}}}}


def _app(billing=None):
    return make_app(store=InMemoryEventStore(),
                    adapters=[CrmSystem(), billing or BillingSystem()],
                    dev_mode=False, admin_token=ADMIN)


async def _agente(app, nombre, tools):
    async with AsyncRemoteRuntime(
            "http://test", admin_token=ADMIN,
            transport=httpx.ASGITransport(app=app)) as adm:
        await adm.enroll_agent(nombre, allowed_tools=tools)
        inst = await adm.spawn_instance(nombre)
        return await adm.issue_credential(inst["instance_id"],
                                          AgentKeyMaterial())


def test_signed_async_flow_end_to_end():
    async def main():
        app = _app()
        creds = await _agente(app, "bot", {"crm.create_customer"})
        async with AsyncRemoteRuntime(
                "http://test", credentials=creds,
                transport=httpx.ASGITransport(app=app)) as rt, AsyncRemoteTask(
                rt, agent="bot", objective="Async enrollment",
                condition=COND, input={"email": EMAIL},
                credentials=creds) as t:
            r = await t.tools.crm.create_customer(
                {"name": "Ana", "email": EMAIL})
            assert r.ok
            v = await t.verify()
            assert v.status == "met"
            assert (await t.audit()).ok
    asyncio.run(main())


def test_without_credentials_the_data_plane_returns_401():
    async def main():
        app = _app()
        async with AsyncRemoteRuntime(
                "http://test", admin_token=ADMIN,
                transport=httpx.ASGITransport(app=app)) as rt:
            with pytest.raises(RemoteError) as exc:
                async with AsyncRemoteTask(
                        rt, agent="bot", objective="o", condition=COND,
                        input={"email": EMAIL}):
                    pass
            assert exc.value.status_code == 401
    asyncio.run(main())


class _PierdeLaPrimeraRespuesta(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self._inner, self.perdidas = inner, 0

    async def handle_async_request(self, request):
        resp = await self._inner.handle_async_request(request)
        if (self.perdidas == 0 and request.url.path.endswith("/invoke")
                and b"billing.create_invoice" in request.content):
            self.perdidas += 1
            raise httpx.ReadTimeout("lost response (simulated)",
                                    request=request)
        return resp

    async def aclose(self):
        await self._inner.aclose()


def test_the_async_client_retries_with_the_same_key():
    async def main():
        billing = BillingSystem()
        app = _app(billing)
        creds = await _agente(
            app, "bot", {"crm.create_customer", "billing.create_invoice"})
        flaky = _PierdeLaPrimeraRespuesta(httpx.ASGITransport(app=app))
        async with AsyncRemoteRuntime("http://test", credentials=creds,
                                      transport=flaky) as rt, AsyncRemoteTask(
                rt, agent="bot", objective="o", condition=COND,
                input={"email": EMAIL}, credentials=creds) as t:
            c = await t.tools.crm.create_customer(
                {"name": "Ana", "email": EMAIL})
            r = await t.tools.billing.create_invoice(
                {"customer_id": c.id, "amount": 100})
            assert flaky.perdidas == 1 and r.ok
        assert len(billing.find({})) == 1
    asyncio.run(main())
