import asyncio

import httpx

from examples.demo_coordinado import condicion_coordinada, plan_3pasos
from examples.demo_identidad import COND, DATOS
from services.app import make_app
from vexinter import Vexinter
from vexinter.core.store import InMemoryEventStore
from vexinter.demo import BillingSystem, CrmSystem, NotifierSystem
from vexinter.identity import AgentKeyMaterial

EMAIL = DATOS["email"]


class ASGISyncTransport(httpx.BaseTransport):
    def __init__(self, app):
        self._transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def send():
            resp = await self._transport.handle_async_request(request)
            
            # Read the response content asynchronously
            body_bytes = await resp.aread()
            
            return httpx.Response(
                status_code=resp.status_code,
                headers=resp.headers,
                content=body_bytes,
                extensions=resp.extensions
            )
        return asyncio.run(send())


def _transport(app):
    return ASGISyncTransport(app)


def _app(adapters, token="tok"):
    return make_app(store=InMemoryEventStore(), adapters=adapters,
                    dev_mode=False, admin_token=token)


def _drive(app, condition=COND):
    """Escenario por HTTP con cliente firmado; devuelve (task_id, admin)."""
    adm = Vexinter(endpoint="http://t", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("a", allowed_tools={
        "crm.create_customer", "billing.create_invoice",
        "notify.send_welcome_email",
        "crm.archive_customer", "billing.void_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("a")
    cred = adm.remote.issue_credential(inst["instance_id"], keys)
    vex = Vexinter(endpoint="http://t", credentials=cred,
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=condition,
                  input={"email": EMAIL}) as t:
        yield t, adm


def _console(app) -> httpx.Client:
    c = httpx.Client(transport=_transport(app),
                     base_url="http://t", follow_redirects=False)
    r = c.post("/console/login", data={"token": "tok"})
    assert r.status_code == 303
    return c


def test_no_cookie_redirects_and_with_token_it_enters():
    app = _app([CrmSystem(), BillingSystem()])
    c = _console(app)
    with httpx.Client(transport=_transport(app)) as client:
        r = client.get("http://t/console", follow_redirects=False)
    assert r.status_code == 303 and "/console/login" in r.headers["location"]
    assert c.get("/console").status_code == 200
    bad = httpx.Client(transport=_transport(app),
                       base_url="http://t", follow_redirects=False)
    r = bad.post("/console/login", data={"token": "no"})
    assert r.status_code == 200 and "incorrect" in r.text


def test_panel_y_tareas_reflejan_el_registro():
    app = _app([CrmSystem(), BillingSystem(fail_first=1)])
    for t, _ in _drive(app):
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.recover(failed_tool="billing.create_invoice")   # reintento conforme
        assert t.verify().status == "met"
    cons = _console(app)
    html = cons.get("/console").text
    assert "satisfied" in html and "Detected false successes" in html
    html = cons.get("/console/tasks").text
    assert "state machine" in html.lower()
    assert "pending_human_decision" in html       # estado listado


def test_explorador_muestra_evidencia_insuficiente_y_auditoria():
    app = _app([CrmSystem(), BillingSystem(read_consistency="eventual")])
    for t, _ in _drive(app):
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "inconclusive"
    with httpx.Client(transport=_transport(app)) as client:
        r = client.get("http://t/v1/tasks?state=inconclusive",
                       headers={"X-Vex-Admin": "tok"})
    task_id = r.json()[0]["task_id"]
    cons = _console(app)
    html = cons.get(f"/console/tasks/{task_id}").text
    assert "Insufficient evidence" in html      # explicit §13.2
    assert "Intact chain" in html
    assert "verifier.verdict" in html


def test_cola_preview_y_decision_por_consola():
    app = _app([CrmSystem(), BillingSystem(fail_first=1,
                                         supports_idempotency=False)])
    for t, _ in _drive(app):
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status.value == "waiting_human"
    cons = _console(app)
    html = cons.get("/console/cases").text
    assert case.id[:18] in html and "Check current state" in html

    r = cons.get(f"/console/api/cases/{case.id}/preview")
    pv = r.json()
    assert pv["status_now"] == "not_met"
    assert pv["would_skip_execution"] is False

    r = cons.post(f"/console/cases/{case.id}/decide",
                  data={"action": "close", "operator": "tester"})
    assert r.status_code == 303
    assert "No cases pending human decision" in cons.get("/console/cases").text


def test_preview_marca_que_no_se_ejecutaria_si_el_estado_cambio():
    """§5.4 visible: the synchronizer created the invoice while waiting."""
    from vexinter.demo import BillingSystem as B
    billing = B(fail_first=1, supports_idempotency=False)
    app = _app([CrmSystem(), billing])
    for t, _ in _drive(app):
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        billing.simulate_external_sync({"customer_id": c.id, "amount": 100})
    cons = _console(app)
    pv = cons.get(f"/console/api/cases/{case.id}/preview").json()
    assert pv["status_now"] == "met"
    assert pv["would_skip_execution"] is True     # la UI avisa ANTES de decidir


def test_grafo_dibuja_compensacion_y_estado_final():
    app = _app([CrmSystem(), BillingSystem(), NotifierSystem()])
    for t, adm in _drive(app, condition=condicion_coordinada()):
        v = t.run_plan(plan_3pasos())
        assert v.status == "not_met"
    cons = _console(app)
    import json as _j
    with httpx.Client(transport=_transport(app)) as client:
        r = client.get("http://t/v1/tasks?state=failed",
                       headers={"X-Vex-Admin": "tok"})
    task_id = _j.loads(r.text)[0]["task_id"]
    html = cons.get(f"/console/tasks/{task_id}").text
    assert "e-comp" in html                       # the «compensated» edge in the SVG
    assert "compenso_a" in html or "compens" in html
    assert "Intact chain" in html                # §7.3: historia no borrada


def test_administracion_de_agentes_por_consola():
    app = _app([CrmSystem(), BillingSystem()])
    cons = _console(app)
    r = cons.post("/console/agents/enroll", data={
        "agent_id": "nuevo", "display_name": "Nuevo",
        "allowed_tools": "crm.create_customer", "allowed_recipients": ""})
    assert r.status_code == 303
    html = cons.get("/console/agents").text
    assert "nuevo" in html and "crm.create_customer" in html
    # credential revocation from the console
    adm = Vexinter(endpoint="http://t", admin_token="tok",
                   transport=_transport(app))
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("nuevo")
    cred = adm.remote.issue_credential(inst["instance_id"], keys)
    r = cons.post(f"/console/agents/credentials/{cred.credential_id}/revoke",
                  data={"reason": "prueba"})
    assert r.status_code == 303
    html = cons.get("/console/agents").text
    assert "revoked" in html


def test_admin_summary_y_detalle_completo():
    """Fija la superficie REST que consume la consola React."""
    app = _app([CrmSystem(), BillingSystem(fail_first=1)])
    for t, _ in _drive(app):
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.recover(failed_tool="billing.create_invoice")
        assert t.verify().status == "met"
    H = {"X-Vex-Admin": "tok"}

    with httpx.Client(
        transport=_transport(app),
        base_url="http://t"
    ) as client:
        s = client.get(
            "/v1/admin/summary",
            headers=H
        ).json()

        assert s["cases"] == 0
        assert s["metrics"]["executions"] == 1

        tid = client.get(
            "/v1/tasks",
            headers=H
        ).json()[0]["task_id"]

        d = client.get(
            f"/v1/admin/tasks/{tid}",
            headers=H
        ).json()

        assert {
            "state", "objective", "verdicts", "events", "graph",
            "audit", "insufficient"
        } <= set(d)
        assert d["state"] == "satisfied"
        assert d["audit"]["ok"]
        assert any(
            e["rel"] == "fue_verificado_por"
            for e in d["graph"]["edges"]
        )
        assert client.get(
            "/v1/identity/credentials",
            headers=H
        ).status_code == 200