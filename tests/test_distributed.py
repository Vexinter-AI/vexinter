import hashlib
import logging
from datetime import UTC, datetime

import httpx
import pytest

from examples.demo_identity import COND, DATA
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

EMAIL = DATA["email"]


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


def _agent(app, admin=False):
    """Da de alta 'agent_a' con credential y devuelve (Vexinter, keys)."""
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


def test_flujo_complete_signed():
    """ steps 1-12 over HTTP, with per-request signing."""
    app = _app()
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"     # fake success detected
        t.recover(failed_tool="billing.create_invoice")
        assert t.verify().status == "met"
        assert t.state is ExecutionState.SATISFIED
        assert t.audit().ok


def test_replay_y_body_altered_rejected():
    """ aplicado al transporte: la same signature no sirve two times ni
    cubre un body different."""
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
    altered = body.replace(b"ana@example.com", b"eva@example.com")                  # body changed
    r3 = raw.post("/v1/tasks", content=altered, headers=headers)
    assert r3.status_code == 401


def test_state_is_rebuilt_after_a_restart():
    """Persistent registry (same store) + NEW service rebuilds
    cases, identidades y works con las credenciales previas."""
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem(fail_first=1,
                                           supports_idempotency=False)]
    app1 = _app(store=store, adapters=adapters)
    vex, _, rc = _agent(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status.value == "waiting_human"

    app2 = _app(store=store, adapters=adapters)      # "restart"
    adm2 = Vexinter(endpoint="http://localhost", admin_token="tok",
                    transport=_transport(app2))
    cases = adm2.remote.pending_cases()
    assert len(cases) == 1                            # the queue survived

    # identity too: the OLD credential authenticates against app2
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app2))
    with vex2.task(agent="a", objective="o2", condition=COND,
                   input={"email": "other@example.com"}) as t2:
        c = t2.tools.crm.create_customer(DATA)  # <--- add this to populate the scope
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
        self.lost = 0

    def handle_request(self, request: Request) -> Response:
        resp = self._inner.handle_request(request)
        if (self.lost == 0 and request.url.path.endswith("/invoke")
                and b"billing.create_invoice" in request.content):
            self.lost += 1
            raise httpx.ReadTimeout("response lost (simulada)",
                                    request=request)
        return resp

    def close(self):
        self._inner.close()


def test_response_lost_not_duplicates_the_operation():
    """ La response se pierde, el customer retries con la same
    key y el service no crea una second invoice."""
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
        c = t.tools.crm.create_customer(DATA)
        r = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert flaky.lost == 1            # the response was indeed lost
        assert r.ok
        assert len(billing.find({})) == 1     # and the invoice was not duplicated
        assert t.verify().status == "met"


def test_recover_remote_accepts_invoked_event():
    """The failed operation is indicated per invocation, not by name."""
    app = _app()
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        r = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"   # fake success
        t.recover(failed_tool="billing.create_invoice",
                  invoked_event=r.provenance.invoked_event)
        assert t.verify().status == "met"

def test_guard_accepts_disorder_y_rejects_repetition():
    """ Arrival order does not matter; repeating a pair is rejected."""
    from vexinter.core.store import InMemorySequenceGuard
    g = InMemorySequenceGuard()
    assert g.check_and_advance("c1", 200)
    assert g.check_and_advance("c1", 100)        # arrives out of order: legitimate
    assert not g.check_and_advance("c1", 200)    # repeat
    assert not g.check_and_advance("c1", 100)
    assert g.check_and_advance("c2", 200)        # other credential: independent


def test_guard_forgets_lo_que_leaves_of_the_window():
    """Past retention the pair is forgotten: the timestamp already rejects it."""
    import time as _time

    from vexinter.core.store import InMemorySequenceGuard
    g = InMemorySequenceGuard(retention_s=0.05)
    assert g.check_and_advance("c", 1)
    assert not g.check_and_advance("c", 1)
    _time.sleep(0.15)
    assert g.check_and_advance("c", 1)


def test_customer_reiniciado_with_reloj_atrasado_is_accepted(monkeypatch):
    """Antes, un customer new con el clock 2 min atrasado quedaba por
    below the server last sequence and was rejected as replay."""
    import time as _t
    import types

    from vexinter import remote
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, rc = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATA)
    # client "restart" + clock skew backwards (within the window)
    monkeypatch.setattr(remote, "time",
                        types.SimpleNamespace(time=lambda: _t.time() - 120))
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app))
    with vex2.task(agent="a", objective="o2", condition=COND,
                   input={"email": EMAIL}) as t2:
        assert t2.tools.crm.create_customer(DATA).ok


def test_plan_remote_not_conclusive_schedules_review():
    """Same over HTTP: /plan schedules the review and run-due closes it."""
    from examples.demo_coordinated import DATA as DATOS_C
    from examples.demo_coordinated import coordinated_condition, plan_two_steps
    billing = BillingSystem(read_consistency="eventual")
    app = make_app(store=InMemoryEventStore(),
                   adapters=[CrmSystem(), billing], dev_mode=False,
                   admin_token="tok", followup_delay_s=0)
    vex, _, _ = _agent(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=coordinated_condition(),
                  input={"email": DATOS_C["email"]}) as t:
        v = t.run_plan(plan_two_steps())
        assert v.status == "inconclusive"
        billing.tick()
        res = adm.remote.run_due_followups()
        assert res[-1]["status"] == "met"
        assert t.state is ExecutionState.SATISFIED


def test_response_lost_in_tool_without_idempotency_not_duplicates():
    """ Although el adapter no deduplique, el service devuelve el
    result already recorded for the same customer key."""
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
        c = t.tools.crm.create_customer(DATA)
        r = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert flaky.lost == 1
        assert r.ok
        assert len(billing.find({})) == 1
        assert sum(1 for e in t.events()
                   if e.type.value == "tool.replayed") == 1
        assert t.audit().ok

def test_provenance_remote_has_the_same_shape_que_the_embedded():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        r = t.tools.crm.create_customer(DATA)
        assert r.provenance.tool == "crm.create_customer"
        ids = {e.id for e in t.events()}
        assert r.provenance.invoked_event in ids
        assert r.provenance.returned_event in ids
        assert "evidence=" in r.to_prompt_block()


class _FlakyRemoto:
    """Primera call: ok=False. Las next: ok=True."""
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
    same tool, el retry human es ambiguous: 409, no 500."""
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

def test_result_unknown_responde_502():
    """A 502 tells the client 'I don't know if it happened'; a 422 would say 'failed'."""
    class _Corta(BillingSystem):
        def invoke(self, tool, req):
            if tool == "create_invoice":
                raise IndeterminateOutcome("no response (simulated)")
            return super().invoke(tool, req)

    app = _app(adapters=[CrmSystem(), _Corta()])
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        with pytest.raises(RemoteError) as exc:
            t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert exc.value.status_code == 502


def test_once_per_execution_also_holds_over_http():
    billing = BillingSystem(once=True)
    app = _app(adapters=[CrmSystem(), billing])
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        a = {"customer_id": c.id, "amount": 100}
        r1 = t.tools.billing.create_invoice(a)
        r2 = t.tools.billing.create_invoice(a)
        assert r1.ok and r2.ok and r1.data["id"] == r2.data["id"]
        assert len(billing.find({})) == 1


def _other_agent(app, name):
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent(name, allowed_tools={
        "crm.create_customer", "billing.create_invoice"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance(name)
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    return Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app))


def test_an_agent_cannot_forge_events_via_the_generic_endpoint():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agent(app)
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request(
            "POST", "/v1/streams/task:ajena/events",
            {"type": "recovery.case_resolved",
             "payload": {"case_id": "false_case"},
             "agent_id": "other_agent"})
    assert exc.value.status_code == 403


def test_the_admin_can_append_events():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    r = adm.remote._http.request(
        "POST", "/v1/streams/mantenimiento/events",
        {"type": "task.assigned", "payload": {"note": "prueba"}}, admin=True)
    assert r["id"] and r["seq"] == 1


def test_an_agent_cannot_read_another_agent_memory():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agent(app)                       # agente "a"
    vb = _other_agent(app, "b")
    with va.task(agent="a", objective="objective private de a",
                 condition=COND, input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATA)
        t.verify()
    with vb.task(agent="b", objective="o", condition=COND,
                 input={"email": "b@example.com"}) as tb:
        with pytest.raises(RemoteError) as exc:
            tb.memory(agent="a")
        assert exc.value.status_code == 403
        assert not any("objective private de a" in str(m)
                       for m in tb.memory())


def test_in_authenticated_mode_an_agent_without_tool_list_is_not_registered():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with pytest.raises(RemoteError) as exc:
        adm.remote.enroll_agent("without_list")
    assert exc.value.status_code == 422
    # an empty list is valid: it means "no tools"
    adm.remote.enroll_agent("without_tools", allowed_tools=set())


def test_an_agent_cannot_see_another_agent_events():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agent(app)                       # agente "a"
    vb = _other_agent(app, "b")
    with va.task(agent="a", objective="tarea private de a", condition=COND,
                 input={"email": EMAIL}) as ta:
        ta.tools.crm.create_customer(DATA)
        task_a = ta.task_id
    with vb.task(agent="b", objective="o", condition=COND,
                 input={"email": "b@example.com"}):
        propios = vb.remote._http.request("GET", "/v1/events")
        assert propios                             # ve lo suyo
        assert all(e.get("task_id") != task_a for e in propios)
        with pytest.raises(RemoteError) as exc:
            vb.remote._http.request("GET", "/v1/events",
                                    params={"task_id": task_a})
        assert exc.value.status_code == 403


def _agent_with_keys(app, name):
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.enroll_agent(name, allowed_tools={"crm.create_customer"})
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance(name)
    rc = adm.remote.issue_credential(inst["instance_id"], keys)
    return Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app)), rc


def test_the_remote_receiver_verifies_the_signature_and_gets_the_message():
    from vexinter.identity import build_envelope_for
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, rca = _agent_with_keys(app, "a")
    vb, rcb = _agent_with_keys(app, "b")
    env = build_envelope_for(
        va.remote.encryption_pub("b"), sender_keys=rca.keys,
        from_agent="a", from_instance=rca.instance_id,
        credential_id=rca.credential_id, to_agent="b", plaintext="hola b",
        sequence=va.remote.message_sequence("a", "b"))
    va.remote.message_send(env)
    assert vb.remote.receive_all(rcb.keys) == [b"hola b"]


def test_an_agent_rotates_its_credential_over_http():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, rca = _agent_with_keys(app, "a")
    new = va.remote.rotate_credentials()
    assert new.credential_id != rca.credential_id
    vn = Vexinter(endpoint="http://localhost", credentials=new,
                  transport=_transport(app))
    vold = Vexinter(endpoint="http://localhost", credentials=rca,
                    transport=_transport(app))
    for v in (vn, vold):               # during grace, both authenticate
        with v.task(agent="a", objective="o", condition=COND,
                    input={"email": EMAIL}):
            pass
    with pytest.raises(RemoteError) as exc:   # the replaced one can no longer rotate
        vold.remote.rotate_credentials()
    assert exc.value.status_code == 422


def test_after_grace_previous_credential_stops_authenticating(monkeypatch):
    from datetime import timedelta

    import vexinter.identity.service as svc_mod
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, rca = _agent_with_keys(app, "a")
    new = va.remote.rotate_credentials()
    vold = Vexinter(endpoint="http://localhost", credentials=rca,
                    transport=_transport(app))
    vn = Vexinter(endpoint="http://localhost", credentials=new,
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
    vex, _, rc = _agent(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        task_id = t.task_id
    app2 = _app(store=store, adapters=adapters)         # "restart"
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app2))
    r = vex2.remote._http.request("GET", f"/v1/tasks/{task_id}")
    assert r["agent"] == "a"


def test_the_admin_task_list_shows_the_agent():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agent(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}):
        pass
    filas = adm.remote._http.request("GET", "/v1/tasks", admin=True)
    assert [f["agent"] for f in filas] == ["a"]


def test_an_already_met_review_is_not_restored_after_restart():
    from examples.demo_coordinated import DATA as DATOS_C
    from examples.demo_coordinated import coordinated_condition, plan_two_steps
    store = InMemoryEventStore()
    billing = BillingSystem(read_consistency="eventual")
    adapters = [CrmSystem(), billing]

    def levantar():
        return make_app(store=store, adapters=adapters, dev_mode=False,
                        admin_token="tok", followup_delay_s=0)

    app1 = levantar()
    vex, _, _ = _agent(app1)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app1))
    with vex.task(agent="a", objective="o", condition=coordinated_condition(),
                  input={"email": DATOS_C["email"]}) as t:
        assert t.run_plan(plan_two_steps()).status == "inconclusive"
        billing.tick()
        assert adm.remote.run_due_followups()[-1]["status"] == "met"
    app2 = levantar()
    assert app2.state.runtime.scheduler._pending == []


def test_only_se_restores_a_review_by_execution():
    from vexinter.core.events import EventDraft, EventType
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem()]
    app1 = _app(store=store, adapters=adapters)
    vex, _, _ = _agent(app1)
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


def _app_private(store=None):
    from vexinter.privacy import InMemoryKeyRing, PrivacyStore
    ps = store or PrivacyStore(InMemoryEventStore(), InMemoryKeyRing())
    app = make_app(store=ps, adapters=[CrmSystem(), BillingSystem()],
                   dev_mode=False, admin_token="tok")
    return ps, app


def _task_finished(vex, email=EMAIL):
    with vex.task(agent="a", objective="Alta de Ana", condition=COND,
                  input={"email": email}) as t:
        c = t.tools.crm.create_customer({**DATA, "email": email})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        return t.task_id, t.execution_id


def test_the_admin_can_delete_a_finished_execution():
    import json

    from vexinter.core.store import EventQuery
    ps, app = _app_private()
    vex, _, _ = _agent(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    tid, eid = _task_finished(vex)
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


def test_not_se_deletes_a_execution_in_progress():
    _ps, app = _app_private()
    vex, _, _ = _agent(app)
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
    _ps, app = _app_private()
    vex, _, _ = _agent(app)
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request("POST", "/v1/privacy/erase",
                                 {"execution_ids": ["exec_x"]})
    assert exc.value.status_code == 403


def test_a_deleted_execution_is_not_restored_after_restart():
    ps, app = _app_private()
    vex, _, rc = _agent(app)
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    t1, e1 = _task_finished(vex)
    t2, _e2 = _task_finished(vex, "other@example.com")
    adm.remote._http.request("POST", "/v1/privacy/erase",
                             {"execution_ids": [e1]}, admin=True)
    _, app2 = _app_private(store=ps)                    # "restart"
    runs = app2.state.runtime.runs
    assert t1 not in runs and t2 in runs
    vex2 = Vexinter(endpoint="http://localhost", credentials=rc,
                    transport=_transport(app2))
    assert vex2.remote._http.request(
        "GET", f"/v1/tasks/{t2}")["agent"] == "a"


def test_deletion_y_restart_of_service_contra_postgres():
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
        name = f"ag_{uuid.uuid4().hex[:8]}"
        app = levantar()
        vex = _other_agent(app, name)
        adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                       transport=_transport(app))
        e1 = f"pg1-{uuid.uuid4().hex[:8]}@example.com"
        e2 = f"pg2-{uuid.uuid4().hex[:8]}@example.com"
        t1, x1 = _task_finished(vex, e1)
        t2, x2 = _task_finished(vex, e2)
        r = adm.remote._http.request(
            "POST", "/v1/privacy/erase",
            {"execution_ids": [x1], "reason": "pedido del titular"},
            admin=True)
        assert r["erased"] == [x1]

        app2 = levantar()                                # "restart"
        runs = app2.state.runtime.runs
        assert t1 not in runs and t2 in runs
        assert runs[t2].agent_id == name
        vex2 = Vexinter(endpoint="http://localhost",
                        credentials=vex.remote._http._creds,
                        transport=_transport(app2))
        assert vex2.remote._http.request(
            "GET", f"/v1/tasks/{t2}")["agent"] == name

        with inner._pool.connection() as conn:           # lo guardado en la base
            n = conn.execute(
                "SELECT count(*) FROM vex_event WHERE execution_id = ANY(%s) "
                "AND (position(%s in payload::text) > 0 "
                "OR position(%s in payload::text) > 0)",
                ([x1, x2], e1, e2)).fetchone()[0]
        assert n == 0
    finally:
        inner.close()


def _app_with_operators():
    return make_app(
        store=InMemoryEventStore(),
        adapters=[CrmSystem(), BillingSystem(fail_first=1,
                                             supports_idempotency=False)],
        dev_mode=False, admin_token="tok",
        operators={"ana": "token-ana-12345", "luis": "token-luis-1234"})


def _pending_case(app):
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        return t.recover(failed_tool="billing.create_invoice").id


def _decisions(app):
    from vexinter.core.events import EventType
    from vexinter.core.store import EventQuery
    return [e.payload for e in app.state.runtime.store.query(
        EventQuery(types={EventType.HUMAN_DECISION_MADE}, limit=1000))]


def test_the_human_decision_is_attributed_to_the_token_operator():
    app = _app_with_operators()
    case = _pending_case(app)
    ana = Vexinter(endpoint="http://localhost",
                   admin_token="token-ana-12345", transport=_transport(app))
    ana.remote.decide(case, "reverify", operator="luis")    # el customer miente
    assert [d["operator"] for d in _decisions(app)] == ["ana"]


def test_an_unknown_token_cannot_decides_and_the_admin_one_shows_as_admin():
    app = _app_with_operators()
    case = _pending_case(app)
    intruso = Vexinter(endpoint="http://localhost",
                       admin_token="token-que-no-existe",
                       transport=_transport(app))
    with pytest.raises(RemoteError) as exc:
        intruso.remote.decide(case, "reverify")
    assert exc.value.status_code == 403
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app))
    adm.remote.decide(case, "reverify", operator="cualquiera")
    assert [d["operator"] for d in _decisions(app)] == ["admin"]


def test_privacy_deletion_records_who_requested_it():
    from vexinter.core.events import EventType
    from vexinter.core.store import EventQuery
    from vexinter.privacy import InMemoryKeyRing, PrivacyStore
    ps = PrivacyStore(InMemoryEventStore(), InMemoryKeyRing())
    app = make_app(store=ps, adapters=[CrmSystem(), BillingSystem()],
                   dev_mode=False, admin_token="tok",
                   operators={"ana": "token-ana-12345"})
    vex, _, _ = _agent(app)
    _tid, eid = _task_finished(vex)
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
    app = _app_with_operators()
    case = _pending_case(app)
    c = TestClient(app)
    assert c.post("/console/login",
                  data={"token": "token-ana-12345"}).status_code == 200
    c.post(f"/console/cases/{case}/decide",
           data={"action": "reverify", "operator": "luis"})
    assert [d["operator"] for d in _decisions(app)] == ["ana"]
    no_session = TestClient(app).post(
        f"/console/cases/{case}/decide", data={"action": "reverify"},
        follow_redirects=False)
    assert no_session.status_code == 303                  # al login
    assert len(_decisions(app)) == 1


def test_a_closed_case_does_not_resurrect_after_restart():
    store = InMemoryEventStore()
    adapters = [CrmSystem(), BillingSystem(fail_first=1,
                                           supports_idempotency=False)]
    app1 = _app(store=store, adapters=adapters)
    vex, _, _ = _agent(app1)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status.value == "waiting_human"
    adm = Vexinter(endpoint="http://localhost", admin_token="tok",
                   transport=_transport(app1))
    adm.remote.decide(case.id, "close")
    app2 = _app(store=store, adapters=adapters)            # "restart"
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
    ctx.bindings["customer"] = {"id": "x"}
    node = {"op": "field_equals", "from": "customer", "path": "id",
            "equals": {"$ref": "input.no_existe"}}
    r = eval_condition(node, ctx, SourceRouter([CrmSystem()]))
    assert r.outcome == "inconclusive"
    assert r.reason == "unresolved_reference"


def test_the_verdict_does_not_persist_compared_values():
    from vexinter.core.events import EventType
    from vexinter.core.store import EventQuery
    cond = {"op": "all", "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "customer"},
        {"op": "field_equals", "from": "customer", "path": "email",
         "equals": {"$ref": "input.email"}}]}
    store = InMemoryEventStore()
    app = _app(store=store, adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=cond,
                  input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATA)
        assert t.verify().status == "met"
        tid = t.task_id
    veredictos = [e for e in store.query(EventQuery(task_id=tid, limit=10_000))
                  if e.type == EventType.VERIFIER_VERDICT]
    assert veredictos and EMAIL not in str([e.payload for e in veredictos])


def test_an_agent_cannot_invoke_tools_in_another_agent_task():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agent(app)
    vb = _other_agent(app, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        with pytest.raises(RemoteError) as exc:
            vb.remote._http.request(
                "POST", f"/v1/tasks/{ta.task_id}/invoke",
                {"tool": "crm.create_customer", "args": DATA})
        assert exc.value.status_code == 403


def test_not_se_invokes_in_a_execution_completed():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, _ = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATA)
        tid = t.task_id
    with pytest.raises(RemoteError) as exc:
        vex.remote._http.request(
            "POST", f"/v1/tasks/{tid}/invoke",
            {"tool": "crm.create_customer", "args": DATA})
    assert exc.value.status_code == 409
    with pytest.raises(RemoteError) as exc2:
        vex.remote._http.request("POST", f"/v1/tasks/{tid}/complete", {})
    assert exc2.value.status_code == 409


def test_a_collaborator_authorized_by_the_owner_can_invoke():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agent(app)
    vb = _other_agent(app, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        va.remote._http.request(
            "POST", f"/v1/tasks/{ta.task_id}/collaborators", {"agent": "b"})
        r = vb.remote._http.request(
            "POST", f"/v1/tasks/{ta.task_id}/invoke",
            {"tool": "crm.create_customer", "args": DATA})
        assert r["ok"]
        assert any(e.payload.get("invoked_by", {}).get("agent_id") == "b"
                   for e in ta.events() if e.type.value == "tool.invoked")


def test_only_the_owner_agrega_collaborators():
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    va, _, _ = _agent(app)
    vb = _other_agent(app, "b")
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
    va, _, _ = _agent(app1)
    _other_agent(app1, "b")
    with va.task(agent="a", objective="o", condition=COND,
                 input={"email": EMAIL}) as ta:
        va.remote._http.request(
            "POST", f"/v1/tasks/{ta.task_id}/collaborators", {"agent": "b"})
        tid = ta.task_id
    app2 = _app(store=store, adapters=adapters)
    run = app2.state.runtime.runs[tid]
    assert run.collaborators == ["b"] and run.completed is True

def _signed_post(app, path, payload, creds, extra_headers=None):
    """POST signed how agente (same material que RemoteHttp.request)."""
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
    """M-9: un body >256KiB se rejects con 422 before de validar/observar."""
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    _, _, rc = _agent(app)
    r = _signed_post(app, "/v1/tasks",
                     {"agent": "a", "objective": "x" * 300_000,
                      "condition": COND, "input": {"email": EMAIL}}, rc)
    assert r.status_code == 422


def test_apply_lesson_failure_requires_admin():
    """M-10: an agent cannot downgrade other agents' lessons with success=False."""
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    _, _, rc = _agent(app)
    path = "/v1/memory/lessons/lesson_x/apply"
    assert _signed_post(app, path, {"success": False}, rc).status_code == 403
    assert _signed_post(app, path, {"success": True}, rc).status_code == 200
    ok = _signed_post(app, path, {"success": False}, rc,
                      {"X-Vex-Admin": "tok"})
    assert ok.status_code == 200


def test_in_memory_store_warns_envelope_per_process_replay_guard(caplog):
    """M-8: without Postgres anti-replay is per-process — it must be warned about."""
    import logging as _l
    with caplog.at_level(_l.WARNING, logger="services.app"):
        make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                 dev_mode=False, admin_token="tok")
    assert any("anti-replay" in r.message for r in caplog.records)


def test_hitl_decides_requires_admin_operator():
    """B-3: without the admin token the HITL decision is rejected; with admin it passes auth."""
    app = _app(adapters=[CrmSystem(), BillingSystem()])
    vex, _, rc = _agent(app)
    with vex.task(agent="a", objective="o", condition=COND,
                  input={"email": EMAIL}) as t:
        tid = t.task_id
    path = f"/v1/tasks/{tid}/hitl/decide"
    assert _signed_post(app, path, {"case_id": "c1", "action": "approve"},
                        rc).status_code == 403
    r = _signed_post(app, path, {"case_id": "c1", "action": "approve"}, rc,
                     {"X-Vex-Admin": "tok"})
    assert r.status_code == 404  # auth OK; case doesn't exist
