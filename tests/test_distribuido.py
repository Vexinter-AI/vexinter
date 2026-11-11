import hashlib
import logging
from datetime import UTC, datetime

import httpx
import pytest

from examples.demo_identidad import COND, DATOS
from services.app import make_app
from vexinter import Vexinter
from vexinter.core.crypto import canonical_json
from vexinter.core.statemachine import ExecutionState
from vexinter.core.store import InMemoryEventStore
from vexinter.core.tools import (
    IndeterminateOutcome,
    Observation,
    OperationClass,
    OperationResult,
    ToolSpec,
)
from vexinter.demo import BillingSystem, CrmSystem
from vexinter.identity import AgentKeyMaterial
from vexinter.remote import RemoteError

_log = logging.getLogger(__name__)

EMAIL = DATOS["email"]


def _app(store=None, adapters=None):
    return make_app(store=store or InMemoryEventStore(),
                    adapters=adapters or [CrmSystem(),
                                          BillingSystem(fail_first=1)],
                    dev_mode=False, admin_token="tok")


import asyncio

from httpx import BaseTransport, ByteStream, Request, Response


class SyncASGITransport(BaseTransport):
    def __init__(self, app):
        self._transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: Request) -> Response:
        async def send():
            # Force base_url or ensure the request has a valid complete path
            res = await self._transport.handle_async_request(request)
            body = await res.aread()
            return res.status_code, res.headers.raw, body

        status_code, headers, body = asyncio.run(send())
        return Response(
            status_code=status_code,
            headers=headers,
            stream=ByteStream(body),
            request=request,
        )

    def close(self):
        try:
            asyncio.run(self._transport.aclose())
        except Exception as e:
            _log.debug("error cerrando transporte ASGI de test: %s", e)

def _transport(app):
    return SyncASGITransport(app=app)


def _agente(app, admin=False):
    """Da de alta 'agente_a' con credencial y devuelve (Vexinter, claves)."""
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("a", allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("a")
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    vex = Vexinter(endpoint="http://localhost", credentials=rc,
                   transport=_transport(app))
    return (adm if admin else vex), keys, rc


def test_flujo_completo_firmado():
    """§10 steps 1-12 over HTTP, with per-request signing."""
    app = _app()
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"     # fake success detected
        t.recover(failed_tool="billing.create_invoice")
        assert t.verify().status == "met"
        assert t.state is ExecutionState.SATISFIED
        assert t.audit().ok


def test_replay_y_cuerpo_alterado_rechazados():
    """§9.4 aplicado al transporte: la misma firma no sirve dos veces ni
    cubre un cuerpo distinto."""
    import time as _time
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("b", allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    kb = AgentKeyMaterial()
    instb = adm.remote.spawn_instance("b")
    rcb = adm.remote.issue_credential(instb["instance_id"], kb)

    raw = httpx.Client(transport=_transport(app), base_url="http://localhost")
    body = canonical_json({"agent": "b", "objective": "o", "condition": COND,
                           "input": {"email": EMAIL}}).encode()
    ts = _time.time()
    seq = int(ts * 1000)
    material = f"POST|/v1/tasks|{hashlib.sha256(body).hexdigest()}|{ts}|{seq}"
    headers = {
            "Content-Type": "application/json",
            "X-Vex-Credential": rcb.credential_id,
            "X-Vex-Timestamp": str(ts),
            "X-Vex-Sequence": str(seq),
            "X-Vex-Signature": kb.sign(material.encode())
        }
    r1 = raw.post("/v1/tasks", content=body, headers=headers)
    assert r1.status_code == 200
    r2 = raw.post("/v1/tasks", content=body, headers=headers)   # replay
    assert r2.status_code == 401
    altered = body.replace(b"ana@ejemplo.com", b"eva@ejemplo.com")                  # cuerpo cambiado
    r3 = raw.post("/v1/tasks", content=altered, headers=headers)
    assert r3.status_code == 401


def test_state_is_rebuilt_after_a_restart():
    """§15: registro persistente (mismo store) + servicio NUEVO reconstruye
    casos, identidades y funciona con las credenciales previas."""
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem(fail_first=1,
                                           supports_idempotency=False)]
    app1 = _app(store=store, adapters=adapters)
    vex, _, rc = _agente(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status.value == "waiting_human"

    app2 = _app(store=store, adapters=adapters)      # "reinicio"
    adm2 = Vexinter(endpoint="http://localhost", admin_token="tok",
                    transport=_transport(app2))
    cases = adm2.remote.pending_cases()
    assert len(cases) == 1                            # the queue survived

    # identity too: the OLD credential authenticates against app2
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app2))
    with vex2.task(agent="a", objective="o2", condition=COND,
                   input={"email": "otro@ejemplo.com"}) as t2:
        c = t2.tools.crm.create_customer(DATOS)  # <--- add this to populate the scope
        assert t2.verify().status in ("not_met", "inconclusive")

    # and the human decision runs on the restored case
    cid = cases[0]["case"]["id"]
    adm2.remote.decide(cid, "close")
    assert adm2.remote.pending_cases() == []

class _PierdeLaPrimeraRespuesta(BaseTransport):
    """Delivers the request to the service (which executes it) and discards
    the response of the first billing call: the client sees a timeout."""

    def __init__(self, inner):
        self._inner = inner
        self.perdidas = 0

    def handle_request(self, request: Request) -> Response:
        resp = self._inner.handle_request(request)
        if (self.perdidas == 0 and request.url.path.endswith("/invoke")
                and b"billing.create_invoice" in request.content):
            self.perdidas += 1
            raise httpx.ReadTimeout("respuesta perdida (simulada)",
                                    request=request)
        return resp

    def close(self):
        self._inner.close()


def test_respuesta_perdida_no_duplica_la_operacion():
    """§2.1: la respuesta se pierde, el cliente reintenta con la misma
    clave y el servicio no crea una segunda factura."""
    billing = BillingSystem()
    app = _app(adapters=[CrmSystem(), billing])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("a", allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("a")
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    flaky = _PierdeLaPrimeraRespuesta(_transport(app))
    vex = Vexinter(endpoint="http://localhost", credentials=rc,
                   transport=flaky)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        r = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert flaky.perdidas == 1            # the response was indeed lost
        assert r.ok
        assert len(billing.find({})) == 1     # and the invoice was not duplicated
        assert t.verify().status == "met"


def test_recover_remoto_acepta_invoked_event():
    """The failed operation is indicated per invocation, not by name."""
    app = _app()
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        r = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"   # fake success
        t.recover(failed_tool="billing.create_invoice",
                  invoked_event=r.provenance.invoked_event)
        assert t.verify().status == "met"

def test_guard_acepta_desorden_y_rechaza_repeticion():
    """§9.4: arrival order does not matter; repeating a pair is rejected."""
    from vexinter.core.store import InMemorySequenceGuard
    g = InMemorySequenceGuard()
    assert g.check_and_advance("c1", 200)
    assert g.check_and_advance("c1", 100)        # arrives out of order: legitimate
    assert not g.check_and_advance("c1", 200)    # repeat
    assert not g.check_and_advance("c1", 100)
    assert g.check_and_advance("c2", 200)        # otra credencial: independiente


def test_guard_olvida_lo_que_sale_de_la_ventana():
    """Past retention the pair is forgotten: the timestamp already rejects it."""
    import time as _time

    from vexinter.core.store import InMemorySequenceGuard
    g = InMemorySequenceGuard(retention_s=0.05)
    assert g.check_and_advance("c", 1)
    assert not g.check_and_advance("c", 1)
    _time.sleep(0.15)
    assert g.check_and_advance("c", 1)


def test_cliente_reiniciado_con_reloj_atrasado_es_aceptado(monkeypatch):
    """Antes, un cliente nuevo con el reloj 2 min atrasado quedaba por
    below the server last sequence and was rejected as replay."""
    import time as _t
    import types

    from vexinter import remote
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, rc = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATOS)
    # client "restart" + clock skew backwards (within the window)
    monkeypatch.setattr(remote, "time",
                        types.SimpleNamespace(time=lambda: _t.time() - 120))
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app))
    with vex2.task(agent="a", objective="o2", condition=COND,
                   input={"email": EMAIL}) as t2:
        assert t2.tools.crm.create_customer(DATOS).ok


def test_plan_remoto_no_concluyente_agenda_revision():
    """Same over HTTP: /plan schedules the review and run-due closes it."""
    from examples.demo_coordinado import DATOS as DATOS_C
    from examples.demo_coordinado import condicion_coordinada, plan_2pasos
    billing = BillingSystem(read_consistency="eventual")
    app = make_app(store=InMemoryEventStore(),
                   adapters=[CrmSystem(), billing], dev_mode=False,
                   admin_token="tok", followup_delay_s=0)
    vex, _, _ = _agente(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=condicion_coordinada(),
                  input={"email": DATOS_C["email"]}) as t:
        v = t.run_plan(plan_2pasos())
        assert v.status == "inconclusive"
        billing.tick()
        res = adm.remote.run_due_followups()
        assert res[-1]["status"] == "met"
        assert t.state is ExecutionState.SATISFIED


def test_respuesta_perdida_en_herramienta_sin_idempotencia_no_duplica():
    """§2.1: aunque el adaptador no deduplique, el servicio devuelve el
    resultado ya registrado para la misma clave de cliente."""
    billing = BillingSystem(supports_idempotency=False)
    app = _app(adapters=[CrmSystem(), billing])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("a", allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("a")
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    flaky = _PierdeLaPrimeraRespuesta(_transport(app))
    vex = Vexinter(endpoint="http://localhost", credentials=rc,
                   transport=flaky)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        r = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert flaky.perdidas == 1
        assert r.ok
        assert len(billing.find({})) == 1
        assert sum(1 for e in t.events()
                   if e.type.value == "tool.replayed") == 1
        assert t.audit().ok

def test_provenance_remota_tiene_la_misma_forma_que_la_embebida():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        r = t.tools.crm.create_customer(DATOS)
        assert r.provenance.tool == "crm.create_customer"
        ids = {e.id for e in t.events()}
        assert r.provenance.invoked_event in ids
        assert r.provenance.returned_event in ids
        assert "evidencia=" in r.to_prompt_block()


class _FlakyRemoto:
    """Primera llamada: ok=False. Las siguientes: ok=True."""
    system = "flaky"

    def __init__(self):
        self.calls = 0

    def tools(self):
        return {"do": ToolSpec(system="flaky", name="do",
                               operation_class=OperationClass.IDEMPOTENT_WRITE,
                               supports_idempotency=True)}

    def invoke(self, tool, req):
        self.calls += 1
        if self.calls == 1:
            return OperationResult(ok=False, error="timeout")
        return OperationResult(ok=True, data={"n": self.calls})

    def observe(self, query):
        return Observation(data=None, observed_at=datetime.now(UTC),
                           source="flaky")


def test_deciding_ambiguous_retry_returns_409_not_500():
    """If while the case waited another distinct operation appeared
    misma herramienta, el reintento humano es ambiguo: 409, no 500."""
    app = _app(adapters=[CrmSystem(), BillingSystem(), _FlakyRemoto()])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent("a", allowed_tools={"flaky.do"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("a")
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    vex = Vexinter(endpoint="http://localhost", credentials=rc,
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        t.tools.flaky.do({"x": 1})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="flaky.do")
        assert case.status.value == "waiting_human"
        opciones = [o["action"] for c in adm.remote.pending_cases()
                    for o in c["options"]]
        assert "retry" in opciones
        t.tools.flaky.do({"x": 2})          # another distinct logical operation
        with pytest.raises(RemoteError) as exc:
            adm.remote.decide(case.id, "retry")
        assert exc.value.status_code == 409

def test_resultado_desconocido_responde_502():
    """A 502 tells the client 'I don't know if it happened'; a 422 would say 'failed'."""
    class _Corta(BillingSystem):
        def invoke(self, tool, req):
            if tool == "create_invoice":
                raise IndeterminateOutcome("sin respuesta (simulado)")
            return super().invoke(tool, req)

    app = _app(adapters=[CrmSystem(), _Corta()])
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        with pytest.raises(RemoteError) as exc:
            t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert exc.value.status_code == 502


def test_once_per_execution_also_holds_over_http():
    billing = BillingSystem(una_vez=True)
    app = _app(adapters=[CrmSystem(), billing])
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        a = {"customer_id": c.id, "amount": 100}
        r1 = t.tools.billing.create_invoice(a)
        r2 = t.tools.billing.create_invoice(a)
        assert r1.ok and r2.ok and r1.data["id"] == r2.data["id"]
        assert len(billing.find({})) == 1


def _otro_agente(app, nombre):
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent(nombre, allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance(nombre)
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    return Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app))


def test_an_agent_cannot_forge_events_via_the_generic_endpoint():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agente(app)
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request(
            "POST", "/v1/streams/task:ajena/events",
            {"type": "recovery.case_resolved",
             "payload": {"case_id": "case_falso"},
             "agent_id": "otro_agente"})
    assert exc.value.status_code == 403


def test_the_admin_can_append_events():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    r = adm.remote._http.request(
        "POST", "/v1/streams/mantenimiento/events",
        {"type": "task.assigned", "payload": {"nota": "prueba"}}, admin=True)
    assert r["id"] and r["seq"] == 1


def test_an_agent_cannot_read_another_agent_memory():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agente(app)                       # agente "a"
    vb = _otro_agente(app, "b")
    with va.task(agent="a", objective="objetivo privado de a",
                 condition=COND, input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATOS)
        t.verify()
    with vb.task(agent="b", objective="o", condition=COND,
                 input={"email": "b@ejemplo.com"}) as tb:
        with pytest.raises(RemoteError) as exc:
            tb.memory(agent="a")
        assert exc.value.status_code == 403
        assert not any("objetivo privado de a" in str(m)
                       for m in tb.memory())


def test_in_authenticated_mode_an_agent_without_tool_list_is_not_registered():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with pytest.raises(RemoteError) as exc:
        adm.remote.enroll_agent("sin_lista")
    assert exc.value.status_code == 422
    # an empty list is valid: it means "no tools"
    adm.remote.enroll_agent("sin_herramientas", allowed_tools=set())


def test_an_agent_cannot_see_another_agent_events():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agente(app)                       # agente "a"
    vb = _otro_agente(app, "b")
    with va.task(agent="a", objective="tarea privada de a", condition=COND,
                 input={"email": EMAIL}) as ta:
        ta.tools.crm.create_customer(DATOS)
        task_a = ta.task_id
    with vb.task(agent="b", objective="o", condition=COND,
                 input={"email": "b@ejemplo.com"}):
        propios = vb.remote._http.request("GET", "/v1/events")
        assert propios                             # ve lo suyo
        assert all(e.get("task_id") != task_a for e in propios)
        with pytest.raises(RemoteError) as exc:
            vb.remote._http.request("GET", "/v1/events",
                                    params={"task_id": task_a})
        assert exc.value.status_code == 403


def _agente_con_claves(app, nombre):
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent(nombre, allowed_tools={"crm.create_customer"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance(nombre)
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    return Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app)), rc


def test_the_remote_receiver_verifies_the_signature_and_gets_the_message():
    from vexinter.identity import build_envelope_for
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, rca = _agente_con_claves(app, "a")
    vb, rcb = _agente_con_claves(app, "b")
    env = build_envelope_for(
        va.remote.encryption_pub("b"), sender_keys=rca.keys,
        from_agent="a", from_instance=rca.instance_id,
        credential_id=rca.credential_id, to_agent="b", plaintext="hola b",
        sequence=va.remote.message_sequence("a", "b"))
    va.remote.message_send(env)
    assert vb.remote.receive_all(rcb.keys) == [b"hola b"]


def test_an_agent_rotates_its_credential_over_http():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, rca = _agente_con_claves(app, "a")
    nuevo = va.remote.rotate_credentials()
    assert nuevo.credential_id != rca.credential_id
    vn = Vexinter(endpoint="http://localhost", credentials=nuevo,
                  transport=_transport(app))
    vold = Vexinter(endpoint="http://localhost", credentials=rca,
                    transport=_transport(app))
    for v in (vn, vold):               # durante la gracia, las dos autentican
        with v.task(agent="a", objective="o", condition=COND,
                    input={"email": EMAIL}):
            pass
    with pytest.raises(RemoteError) as exc:   # la reemplazada ya no puede rotar
        vold.remote.rotate_credentials()
    assert exc.value.status_code == 422


def test_tras_la_gracia_la_credencial_anterior_deja_de_autenticar(monkeypatch):
    from datetime import timedelta

    import vexinter.identity.service as svc_mod
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, rca = _agente_con_claves(app, "a")
    nuevo = va.remote.rotate_credentials()
    vold = Vexinter(endpoint="http://localhost", credentials=rca,
                    transport=_transport(app))
    vn = Vexinter(endpoint="http://localhost", credentials=nuevo,
                  transport=_transport(app))
    monkeypatch.setattr(svc_mod, "now", lambda: datetime.now(UTC)
                        + timedelta(seconds=901))
    with (pytest.raises(RemoteError) as exc,
          vold.task(agent="a", objective="o", condition=COND,
                    input={"email": EMAIL})):
        pass
    assert exc.value.status_code == 401
    with vn.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}):
        pass


def test_an_agent_keeps_its_tasks_after_a_restart():
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem()]
    app1 = _app(store=store, adapters=adapters)
    vex, _, rc = _agente(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        task_id = t.task_id
    app2 = _app(store=store, adapters=adapters)         # "reinicio"
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app2))
    r = vex2.remote._http.request("GET", f"/v1/tasks/{task_id}")
    assert r["agent"] == "a"


def test_the_admin_task_list_shows_the_agent():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agente(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}):
        pass
    filas = adm.remote._http.request("GET", "/v1/tasks", admin=True)
    assert [f["agent"] for f in filas] == ["a"]


def test_an_already_met_review_is_not_restored_after_restart():
    from examples.demo_coordinado import DATOS as DATOS_C
    from examples.demo_coordinado import condicion_coordinada, plan_2pasos
    store = InMemoryEventStore()
    billing = BillingSystem(read_consistency="eventual")
    adapters = [CrmSystem(), billing]

    def levantar():
        return make_app(store=store, adapters=adapters, dev_mode=False,
                        admin_token="tok", followup_delay_s=0)

    app1 = levantar()
    vex, _, _ = _agente(app1)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app1))
    with vex.task(agent="a", objective="o", condition=condicion_coordinada(),
                  input={"email": DATOS_C["email"]}) as t:
        assert t.run_plan(plan_2pasos()).status == "inconclusive"
        billing.tick()
        assert adm.remote.run_due_followups()[-1]["status"] == "met"
    app2 = levantar()
    assert app2.state.runtime.scheduler._pending == []


def test_solo_se_restaura_una_revision_por_ejecucion():
    from vexinter.core.events import EventDraft, EventType
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem()]
    app1 = _app(store=store, adapters=adapters)
    vex, _, _ = _agente(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        task_id, exec_id = t.task_id, t.execution_id
    for _ in range(2):
        store.append(EventDraft(
            type=EventType.FOLLOWUP_SCHEDULED, task_id=task_id,
            execution_id=exec_id,
            payload={"reason": "x",
                     "due_at": datetime.now(UTC).isoformat()}),
            stream=f"task:{task_id}")
    app2 = _app(store=store, adapters=adapters)
    assert len(app2.state.runtime.scheduler._pending) == 1


def _app_privada(store=None):
    from vexinter.privacy import InMemoryKeyRing, PrivacyStore
    ps = store or PrivacyStore(InMemoryEventStore(), InMemoryKeyRing())
    app = make_app(store=ps, adapters=[CrmSystem(), BillingSystem()],
                   dev_mode=False, admin_token="tok")
    return ps, app


def _tarea_terminada(vex, email=EMAIL):
    with vex.task(agent="a", objective="Alta de Ana", condition=COND,
                  input={"email": email}) as t:
        c = t.tools.crm.create_customer({**DATOS, "email": email})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        return t.task_id, t.execution_id


def test_the_admin_can_delete_a_finished_execution():
    import json

    from vexinter.core.store import EventQuery
    ps, app = _app_privada()
    vex, _, _ = _agente(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    tid, eid = _tarea_terminada(vex)
    r = adm.remote._http.request(
        "POST", "/v1/privacy/erase",
        {"execution_ids": [eid], "reason": "pedido del titular"}, admin=True)
    assert r["erased"] == [eid] and r["rejected"] == {}
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request("GET", f"/v1/tasks/{tid}")
    assert exc.value.status_code == 404
    leido = json.dumps([e.payload for e in ps.query(
        EventQuery(execution_id=eid, limit=10_000))], default=str)
    assert EMAIL not in leido


def test_no_se_borra_una_ejecucion_en_curso():
    _ps, app = _app_privada()
    vex, _, _ = _agente(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        with pytest.raises(RemoteError) as exc:
            adm.remote._http.request(
                "POST", "/v1/privacy/erase",
                {"execution_ids": [t.execution_id]}, admin=True)
        assert exc.value.status_code == 409


def test_an_agent_cannot_request_deletion():
    _ps, app = _app_privada()
    vex, _, _ = _agente(app)
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request("POST", "/v1/privacy/erase",
                                 {"execution_ids": ["exec_x"]})
    assert exc.value.status_code == 403


def test_a_deleted_execution_is_not_restored_after_restart():
    ps, app = _app_privada()
    vex, _, rc = _agente(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    t1, e1 = _tarea_terminada(vex)
    t2, _e2 = _tarea_terminada(vex, "otra@ejemplo.com")
    adm.remote._http.request("POST", "/v1/privacy/erase",
                             {"execution_ids": [e1]}, admin=True)
    _, app2 = _app_privada(store=ps)                    # "reinicio"
    runs = app2.state.runtime.runs
    assert t1 not in runs and t2 in runs
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app2))
    assert vex2.remote._http.request(
        "GET", f"/v1/tasks/{t2}")["agent"] == "a"


def test_borrado_y_reinicio_del_servicio_contra_postgres():
    import os
    import uuid

    from vexinter.privacy import InMemoryKeyRing, PrivacyStore
    from vexinter.store_pg import PostgresEventStore, init_schema
    dsn = os.environ.get("VEXINTER_TEST_PG_DSN")
    if not dsn:
        pytest.skip("requiere VEXINTER_TEST_PG_DSN")
    inner = PostgresEventStore(dsn)
    init_schema(inner)
    ring = InMemoryKeyRing()

    def levantar():
        return make_app(store=PrivacyStore(inner, ring),
                        adapters=[CrmSystem(), BillingSystem()],
                        dev_mode=False, admin_token="tok")

    try:
        nombre = f"ag_{uuid.uuid4().hex[:8]}"
        app = levantar()
        vex = _otro_agente(app, nombre)
        adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                       transport=_transport(app))
        e1 = f"pg1-{uuid.uuid4().hex[:8]}@ejemplo.com"
        e2 = f"pg2-{uuid.uuid4().hex[:8]}@ejemplo.com"
        t1, x1 = _tarea_terminada(vex, e1)
        t2, x2 = _tarea_terminada(vex, e2)
        r = adm.remote._http.request(
            "POST", "/v1/privacy/erase",
            {"execution_ids": [x1], "reason": "pedido del titular"},
            admin=True)
        assert r["erased"] == [x1]

        app2 = levantar()                                # "reinicio"
        runs = app2.state.runtime.runs
        assert t1 not in runs and t2 in runs
        assert runs[t2].agent_id == nombre
        vex2 = Vexinter(endpoint="http://localhost",
                        credentials=vex.remote._http._creds,
                        transport=_transport(app2))
        assert vex2.remote._http.request(
            "GET", f"/v1/tasks/{t2}")["agent"] == nombre

        with inner._pool.connection() as conn:           # lo guardado en la base
            n = conn.execute(
                "SELECT count(*) FROM vex_event WHERE execution_id = ANY(%s) "
                "AND (position(%s in payload::text) > 0 "
                "OR position(%s in payload::text) > 0)",
                ([x1, x2], e1, e2)).fetchone()[0]
        assert n == 0
    finally:
        inner.close()


def _app_con_operadores():
    return make_app(
        store=InMemoryEventStore(),
        adapters=[CrmSystem(), BillingSystem(fail_first=1,
                                             supports_idempotency=False)],
        dev_mode=False, admin_token="tok",
        operators={"ana": "token-ana-12345", "luis": "token-luis-1234"})


def _caso_pendiente(app):
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        return t.recover(failed_tool="billing.create_invoice").id


def _decisiones(app):
    from vexinter.core.events import EventType
    from vexinter.core.store import EventQuery
    return [e.payload for e in app.state.runtime.store.query(
        EventQuery(types={EventType.HUMAN_DECISION_MADE}, limit=1000))]


def test_the_human_decision_is_attributed_to_the_token_operator():
    app = _app_con_operadores()
    caso = _caso_pendiente(app)
    ana = Vexinter(endpoint="http://localhost",
                   admin_token="token-ana-12345", transport=_transport(app))
    ana.remote.decide(caso, "reverify", operator="luis")    # el cliente miente
    assert [d["operator"] for d in _decisiones(app)] == ["ana"]


def test_an_unknown_token_cannot_decide_and_the_admin_one_shows_as_admin():
    app = _app_con_operadores()
    caso = _caso_pendiente(app)
    intruso = Vexinter(endpoint="http://localhost",
                       admin_token="token-que-no-existe",
                       transport=_transport(app))
    with pytest.raises(RemoteError) as exc:
        intruso.remote.decide(caso, "reverify")
    assert exc.value.status_code == 403
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.decide(caso, "reverify", operator="cualquiera")
    assert [d["operator"] for d in _decisiones(app)] == ["admin"]


def test_privacy_deletion_records_who_requested_it():
    from vexinter.core.events import EventType
    from vexinter.core.store import EventQuery
    from vexinter.privacy import InMemoryKeyRing, PrivacyStore
    ps = PrivacyStore(InMemoryEventStore(), InMemoryKeyRing())
    app = make_app(store=ps, adapters=[CrmSystem(), BillingSystem()],
                   dev_mode=False, admin_token="tok",
                   operators={"ana": "token-ana-12345"})
    vex, _, _ = _agente(app)
    _tid, eid = _tarea_terminada(vex)
    ana = Vexinter(endpoint="http://localhost",
                   admin_token="token-ana-12345", transport=_transport(app))
    ana.remote._http.request(
        "POST", "/v1/privacy/erase",
        {"execution_ids": [eid], "reason": "pedido"}, admin=True)
    borrados = ps.query(EventQuery(types={EventType.PRIVACY_ERASED},
                                   limit=100))
    assert [e.payload["requested_by"] for e in borrados] == ["ana"]


def test_the_console_attributes_the_decision_to_the_session_operator():
    from fastapi.testclient import TestClient
    app = _app_con_operadores()
    caso = _caso_pendiente(app)
    c = TestClient(app)
    assert c.post("/console/login",
                  data={"token": "token-ana-12345"}).status_code == 200
    c.post(f"/console/cases/{caso}/decide",
           data={"action": "reverify", "operator": "luis"})
    assert [d["operator"] for d in _decisiones(app)] == ["ana"]
    sin_sesion = TestClient(app).post(
        f"/console/cases/{caso}/decide", data={"action": "reverify"},
        follow_redirects=False)
    assert sin_sesion.status_code == 303                  # al login
    assert len(_decisiones(app)) == 1


def test_a_closed_case_does_not_resurrect_after_restart():
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem(fail_first=1,
                                           supports_idempotency=False)]
    app1 = _app(store=store, adapters=adapters)
    vex, _, _ = _agente(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status.value == "waiting_human"
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app1))
    adm.remote.decide(case.id, "close")
    app2 = _app(store=store, adapters=adapters)            # "reinicio"
    adm2 = Vexinter(endpoint="http://localhost", admin_token="tok",
                    transport=_transport(app2))
    assert adm2.remote.pending_cases() == []


def test_without_admin_token_the_console_is_disabled(monkeypatch):
    from fastapi.testclient import TestClient
    for v in ("VEXINTER_ADMIN_TOKEN", "VEXINTER_OPERATOR_TOKENS",
              "VEXINTER_DEV_MODE"):
        monkeypatch.delenv(v, raising=False)
    app = make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                   dev_mode=False, admin_token=None)
    assert TestClient(app).get("/console").status_code == 404


def test_an_unresolved_ref_in_field_equals_gives_inconclusive():
    from vexinter.core.conditions import EvalContext, eval_condition
    from vexinter.core.tools import SourceRouter
    ctx = EvalContext({}, {})
    ctx.bindings["cliente"] = {"id": "x"}
    nodo = {"op": "field_equals", "from": "cliente", "path": "id",
            "equals": {"$ref": "input.no_existe"}}
    r = eval_condition(nodo, ctx, SourceRouter([CrmSystem()]))
    assert r.outcome == "inconclusive"
    assert r.reason == "unresolved_reference"


def test_the_verdict_does_not_persist_compared_values():
    from vexinter.core.events import EventType
    from vexinter.core.store import EventQuery
    cond = {"op": "all", "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "cliente"},
        {"op": "field_equals", "from": "cliente", "path": "email",
         "equals": {"$ref": "input.email"}}]}
    store = InMemoryEventStore()
    app = _app(store=store, adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=cond,
                  input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATOS)
        assert t.verify().status == "met"
        tid = t.task_id
    veredictos = [e for e in store.query(EventQuery(task_id=tid, limit=10_000))
                  if e.type == EventType.VERIFIER_VERDICT]
    assert veredictos and EMAIL not in str([e.payload for e in veredictos])


def test_an_agent_cannot_invoke_tools_in_another_agent_task():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agente(app)
    vb = _otro_agente(app, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        with pytest.raises(RemoteError) as exc:
            vb.remote._http.request(
                "POST", f"/v1/tasks/{ta.task_id}/invoke",
                {"tool": "crm.create_customer", "args": DATOS})
        assert exc.value.status_code == 403


def test_no_se_invoca_en_una_ejecucion_completada():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATOS)
        tid = t.task_id
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request(
            "POST", f"/v1/tasks/{tid}/invoke",
            {"tool": "crm.create_customer", "args": DATOS})
    assert exc.value.status_code == 409
    with pytest.raises(RemoteError) as exc2:
        vex.remote._http.request("POST", f"/v1/tasks/{tid}/complete", {})
    assert exc2.value.status_code == 409


def test_a_collaborator_authorized_by_the_owner_can_invoke():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agente(app)
    vb = _otro_agente(app, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        va.remote._http.request(
            "POST", f"/v1/tasks/{ta.task_id}/collaborators", {"agent": "b"})
        r = vb.remote._http.request(
            "POST", f"/v1/tasks/{ta.task_id}/invoke",
            {"tool": "crm.create_customer", "args": DATOS})
        assert r["ok"]
        assert any(e.payload.get("invoked_by", {}).get("agent_id") == "b"
                   for e in ta.events() if e.type.value == "tool.invoked")


def test_solo_el_dueno_agrega_colaboradores():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agente(app)
    vb = _otro_agente(app, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        with pytest.raises(RemoteError) as exc:
            vb.remote._http.request(
                "POST", f"/v1/tasks/{ta.task_id}/collaborators",
                {"agent": "b"})
        assert exc.value.status_code == 403
        with pytest.raises(RemoteError) as exc2:
            va.remote._http.request(
                "POST", f"/v1/tasks/{ta.task_id}/collaborators",
                {"agent": "inexistente"})
        assert exc2.value.status_code == 422


def test_collaborators_and_closure_survive_a_restart():
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem()]
    app1 = _app(store=store, adapters=adapters)
    va, _, _ = _agente(app1)
    _otro_agente(app1, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        va.remote._http.request(
            "POST", f"/v1/tasks/{ta.task_id}/collaborators", {"agent": "b"})
        tid = ta.task_id
    app2 = _app(store=store, adapters=adapters)
    run = app2.state.runtime.runs[tid]
    assert run.collaborators == ["b"] and run.completed is True

def _signed_post(app, path, payload, creds, extra_headers=None):
    """POST firmado como agente (mismo material que RemoteHttp.request)."""
    import time as _t
    raw = httpx.Client(transport=_transport(app), base_url="http://localhost")
    body = canonical_json(payload).encode()
    ts = _t.time()
    seq = (int(ts * 1000) << 20) | __import__("secrets").randbits(20)
    material = f"POST|{path}|{hashlib.sha256(body).hexdigest()}|{ts}|{seq}"
    headers = {
        "Content-Type": "application/json",
        "X-Vex-Credential": creds.credential_id,
        "X-Vex-Timestamp": str(ts),
        "X-Vex-Sequence": str(seq),
        "X-Vex-Signature": creds.keys.sign(material.encode()),
    }
    if extra_headers:
        headers.update(extra_headers)
    return raw.post(path, content=body, headers=headers)


def test_create_task_rejects_oversized_payload():
    """M-9: un body >256KiB se rechaza con 422 antes de validar/observar."""
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    _, _, rc = _agente(app)
    r = _signed_post(app, "/v1/tasks",
                     {"agent": "a", "objective": "x" * 300_000,
                      "condition": COND, "input": {"email": EMAIL}}, rc)
    assert r.status_code == 422


def test_apply_lesson_failure_requires_admin():
    """M-10: un agente no puede degradar lecciones ajenas con success=False."""
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    _, _, rc = _agente(app)
    path = "/v1/memory/lessons/lesson_x/apply"
    assert _signed_post(app, path, {"success": False}, rc).status_code == 403
    assert _signed_post(app, path, {"success": True}, rc).status_code == 200
    ok = _signed_post(app, path, {"success": False}, rc,
                      {"X-Vex-Admin": "tok"})
    assert ok.status_code == 200


def test_in_memory_store_warns_about_per_process_replay_guard(caplog):
    """M-8: sin Postgres el anti-replay es por proceso — debe advertirse."""
    import logging as _l
    with caplog.at_level(_l.WARNING, logger="services.app"):
        make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                 dev_mode=False, admin_token="tok")
    assert any("anti-replay" in r.message for r in caplog.records)


def test_hitl_decide_requires_admin_operator():
    """B-3: sin token admin la decisión HITL se rechaza; con admin pasa auth."""
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, rc = _agente(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        tid = t.task_id
    path = f"/v1/tasks/{tid}/hitl/decide"
    assert _signed_post(app, path, {"case_id": "c1", "action": "approve"},
                        rc).status_code == 403
    r = _signed_post(app, path, {"case_id": "c1", "action": "approve"}, rc,
                     {"X-Vex-Admin": "tok"})
    assert r.status_code == 404  # auth OK; case doesn't exist
