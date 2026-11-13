from datetime import UTC, timedelta
from datetime import datetime as _dt

from examples.demo_crm_billing import CONDICION, DATOS
from vexinter import Vexinter
from vexinter.core.coordinated import CoordinatedRecovery
from vexinter.core.coordinator import Step
from vexinter.core.events import EventDraft, EventType, now
from vexinter.core.recovery import CaseStatus
from vexinter.core.store import EventQuery, InMemoryEventStore
from vexinter.core.tools import (
    IndeterminateOutcome,
    Observation,
    OperationClass,
    OperationResult,
    ToolCompensation,
    ToolSpec,
)
from vexinter.demo import BillingAdapter, CrmAdapter


def test_alteracion_del_registro_es_detectada():
    """§3.3: modificar un acontecimiento rompe la cadena."""
    store = InMemoryEventStore()
    store.append(EventDraft(type=EventType.TASK_CREATED, payload={"x": 1}),
                 stream="task:t1")
    store.append(EventDraft(type=EventType.EXECUTION_STARTED), stream="task:t1")
    assert store.verify_chain().ok
    ev = store.query(EventQuery(types={EventType.TASK_CREATED}))[0]
    ev.payload["x"] = 999                                  
    audit = store.verify_chain()
    assert not audit.ok
    assert any("content altered" in p for p in audit.problems)


def test_falso_exito_detectado_y_recuperado_con_idempotencia():
    """§1 + §5.2: fake success is detected and the retry does not duplicate."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(), BillingAdapter(fail_first=1)])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"            
        t.recover(failed_tool="billing.create_invoice")
        assert t.verify().status == "met"
        n = sum(1 for e in t.events()
                if e.type == EventType.TOOL_INVOKED
                and e.payload["tool"] == "billing.create_invoice")
        assert n == 2                                      


def test_replica_atrasada_produce_no_concluyente_y_la_revision_cierra():
    """§4.3 + §4.4: uncertainty is preserved and resolved by review."""
    billing = BillingAdapter(read_consistency="eventual")
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "inconclusive"       
        billing.tick()
        assert t.run_followups(at=now() + timedelta(seconds=61))[-1].status == "met"


def test_human_decision_reverifica_antes_de_ejecutar():
    """§5.4: if state changed while waiting, the action is not executed."""
    billing = BillingAdapter(fail_first=1, supports_idempotency=False)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status == CaseStatus.WAITING_HUMAN
        billing.simulate_external_sync({"customer_id": c.id, "amount": 100})
        v = t.decide(case.id, action="retry")
        assert v.status == "met"
        assert case.status == CaseStatus.RESOLVED


def test_condiciones_maliciosas_son_rechazadas():
    """§4.2: el DSL no permite operaciones arbitrarias."""
    import pytest

    from vexinter.core.conditions import ConditionError, validate_condition
    with pytest.raises(ConditionError):
        validate_condition({"op": "ejecutar_sql", "query": "DROP TABLE"})
    with pytest.raises(ConditionError):
        validate_condition({"op": "exists"})                

def _claves(t, tool):
    return [e.payload["idempotency_key"] for e in t.events()
            if e.type == EventType.TOOL_INVOKED and e.payload["tool"] == tool]


def test_llamadas_identicas_legitimas_tienen_claves_distintas():
    """§5.2: two identical logical operations must not collapse into one."""
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), BillingAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        claves = _claves(t, "billing.create_invoice")
        assert len(claves) == 2
        assert claves[0] != claves[1]


def test_the_retry_reuses_the_original_key():
    """§5.2: the recovery retry keeps the operation key."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(), BillingAdapter(fail_first=1)])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"
        t.recover(failed_tool="billing.create_invoice")
        claves = _claves(t, "billing.create_invoice")
        assert len(claves) == 2
        assert claves[0] == claves[1]

class _FlakyAdapter:
    """Primera llamada: ok=False (timeout). Las siguientes: ok=True."""
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
        return Observation(data=None, observed_at=_dt.now(UTC),
                           source="flaky")


def test_llamada_identica_tras_fallo_reutiliza_la_clave():
    """§5.2: retrying after ok=False is the SAME logical operation; retrying
    after ok=True is a new operation."""
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        t.tools.flaky.do({"x": 1})     # ok=False
        t.tools.flaky.do({"x": 1})     # same operation: same key
        t.tools.flaky.do({"x": 1})     # la anterior fue ok=True: clave nueva
        claves = _claves(t, "flaky.do")
        assert len(claves) == 3
        assert claves[0] == claves[1]
        assert claves[2] != claves[1]

def test_last_invocation_requires_precision_with_distinct_operations():
    """Dos operaciones distintas de la misma herramienta: no se adivina."""
    import pytest

    from vexinter.core.recovery import last_invocation
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        r1 = t.tools.flaky.do({"x": 1})
        r2 = t.tools.flaky.do({"x": 2})
        store, run = t._rt.store, t._run
        with pytest.raises(LookupError):
            last_invocation(store, run, "flaky.do")
        inv1 = last_invocation(store, run, "flaky.do",
                               invoked_event=r1.provenance.invoked_event)
        inv2 = last_invocation(store, run, "flaky.do",
                               invoked_event=r2.provenance.invoked_event)
        assert inv1["args"] == {"x": 1}
        assert inv2["args"] == {"x": 2}


def test_last_invocation_accepts_the_same_retried_operation():
    """Retries with the same key are ONE logical operation: not ambiguous."""
    from vexinter.core.recovery import last_invocation
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        t.tools.flaky.do({"x": 1})      # ok=False
        t.tools.flaky.do({"x": 1})      # misma clave
        inv = last_invocation(t._rt.store, t._run, "flaky.do")
        assert inv["args"] == {"x": 1}

def test_caso_ambiguo_no_ofrece_retry_a_la_persona():
    """If the failed operation is unknown, the 'retry' option is not offered."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(), BillingAdapter(), _FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        t.tools.flaky.do({"x": 1})
        t.tools.flaky.do({"x": 2})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="flaky.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        acciones = {o.action for o in t.case_options(case.id)}
        assert "retry" not in acciones
        assert "reverify" in acciones

def test_conteo_de_reintentos_es_por_operacion_logica():
    """Dos pasos con la misma herramienta no comparten el contador de
    reintentos: se cuenta por clave de idempotencia."""
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        r1 = t.tools.flaky.do({"x": 1})    # paso s1: ok=False
        t.tools.flaky.do({"x": 1})         # reintento de s1 (misma clave)
        r2 = t.tools.flaky.do({"x": 2})    # step s2: distinct operation

        rt = t._rt
        cr = CoordinatedRecovery(rt.store, rt.intermediary, rt.verifier,
                                 rt.router, rt.human_queue, rt.tracker)
        s1 = Step(id="s1", tool="flaky.do")
        s2 = Step(id="s2", tool="flaky.do")
        done = {"s1": {"invoked_event": r1.provenance.invoked_event},
                "s2": {"invoked_event": r2.provenance.invoked_event}}

        assert cr._count_retries(t._run, s1, done) == 1
        assert cr._count_retries(t._run, s2, done) == 0


def test_the_scheduler_does_not_duplicate_reviews_of_one_execution():
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), BillingAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        sch = t._rt.scheduler
        sch.schedule(t._run, 30, "primera")
        sch.schedule(t._run, 30, "segunda")
        agendados = [e for e in t.events()
                     if e.type == EventType.FOLLOWUP_SCHEDULED]
        assert len(agendados) == 1


class _SeCaeAdapter:
    """La primera llamada 'muere' a mitad (el proceso cae): sin retorno."""
    system = "boom"

    def __init__(self):
        self.calls = 0

    def tools(self):
        return {"do": ToolSpec(system="boom", name="do",
                               operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE,
                               supports_idempotency=False)}

    def invoke(self, tool, req):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("process crashed mid-call")
        return OperationResult(ok=True, data={"n": self.calls})

    def observe(self, query):
        return Observation(data=None, observed_at=_dt.now(UTC),
                           source="boom")


def test_invocacion_previa_sin_resultado_no_se_repite_si_no_es_idempotente():
    """Unknown whether the effect happened: no guessing, escalate."""
    import pytest

    from vexinter.core.tools import AmbiguousInvocationError
    adapter = _SeCaeAdapter()
    vex = Vexinter(embedded=True, adapters=[adapter])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(RuntimeError):
            inter.call(ctx, "boom.do", {"x": 1}, client_key="k1")
        with pytest.raises(AmbiguousInvocationError):
            inter.call(ctx, "boom.do", {"x": 1}, client_key="k1")
        assert adapter.calls == 1
        assert inter.call(ctx, "boom.do", {"x": 1}, client_key="k2").ok


def test_replay_solo_cuando_el_resultado_registrado_fue_ok():
    """ok=False: the client retries on purpose and it runs; ok=True:
    se devuelve lo registrado sin llamar de nuevo al adaptador."""
    adapter = _FlakyAdapter()
    vex = Vexinter(embedded=True, adapters=[adapter])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        r1 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")
        assert not r1.ok
        r2 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")
        assert r2.ok and adapter.calls == 2
        r3 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")
        assert r3.ok and adapter.calls == 2          # replay: did not call
        assert r3.provenance.invoked_event == r2.provenance.invoked_event


def test_the_replay_leaves_a_trace_in_the_log_and_the_graph():
    """§3.1: devolver un resultado ya registrado es un hecho auditable."""
    adapter = _FlakyAdapter()
    vex = Vexinter(embedded=True, adapters=[adapter])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")        # ok=False
        r2 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")   # ejecuta
        assert inter.call(ctx, "flaky.do", {"x": 1}, client_key="k").ok  # replay
        replays = [e for e in t.events() if e.type == EventType.TOOL_REPLAYED]
        assert len(replays) == 1
        rp = replays[0]
        assert rp.caused_by == r2.provenance.invoked_event
        assert rp.payload["returned_event"] == r2.provenance.returned_event
        assert "data" not in rp.payload          # no duplica contenido
        assert any(e["rel"] == "se_origino_en"
                   and e["source"] == r2.provenance.invoked_event
                   and e["target"] == rp.id
                   for e in t.graph()["edges"])
        assert t.audit().ok


def test_an_adapter_that_cannot_observe_never_produces_not_met():
    """§6.3: with no possible observation the answer is 'inconclusive'.
    Antes, 'not exists' sobre el notificador daba un 'met' sin base."""
    from vexinter.demo import NotifierSystem
    existe = {"op": "exists", "system": "notify",
              "query": {"type": "email",
                        "where": {"to": {"$ref": "input.email"}}}}
    for cond in (existe, {"op": "not", "arg": existe}):
        vex = Vexinter(embedded=True, adapters=[NotifierSystem(fail=False)])
        with vex.task(agent="a", objective="o", condition=cond,
                      input={"email": DATOS["email"]}) as t:
            assert t.verify().status == "inconclusive"


def test_the_verdict_records_source_and_consistency_of_each_read():
    """§4.1: la evidencia queda en el registro, sin el contenido observado."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(),
                             BillingAdapter(read_consistency="eventual")])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        v = [e for e in t.events()
             if e.type == EventType.VERIFIER_VERDICT][-1]
        evid = v.payload["evidence"]
        assert any(x["source"] == "crm/primary"
                   and x["consistency"] == "strong" for x in evid)
        assert any(x["source"] == "billing/replica-1"
                   and x["consistency"] == "eventual" for x in evid)
        assert all("observed" not in x for x in evid)     # solo hashes
        assert t.audit().ok


def test_a_value_reported_by_the_tool_does_not_count_as_evidence():
    """§4.1: comparar la respuesta de la herramienta consigo misma no
    verifica nada."""
    campo = {"op": "field_equals", "from": "steps",
             "path": "a2.data.status", "equals": "issued"}
    vex = Vexinter(embedded=True, adapters=[CrmAdapter()])
    with vex.task(agent="a", objective="o", condition=campo, input={}) as t:
        v = t._rt.verifier.verify(
            t._run, extra_bindings={"steps": {"a2": {"data": {"status": "issued"}}}})
        assert v.status == "inconclusive"


def test_reserved_names_cannot_be_used_as_bindings():
    import pytest

    from vexinter.core.conditions import ConditionError, validate_condition
    for nombre in ("input", "steps"):
        with pytest.raises(ConditionError):
            validate_condition({"op": "exists", "system": "crm",
                                "query": {}, "as": nombre})

class _FlakyLuegoCorte(_FlakyAdapter):
    """First call: ok=False. Afterwards: no definitive answer."""
    system = "flaky2"

    def tools(self):
        return {"do": ToolSpec(system="flaky2", name="do",
                               operation_class=OperationClass.IDEMPOTENT_WRITE,
                               supports_idempotency=True)}

    def invoke(self, tool, req):
        self.calls += 1
        if self.calls == 1:
            return OperationResult(ok=False, error="timeout")
        raise IndeterminateOutcome("sin respuesta (simulado)")


def test_a_retry_without_answer_escalates_instead_of_breaking():
    """§4.3: si el reintento no obtiene respuesta definitiva, se desconoce
    whether the effect happened: it escalates to a person, it does not propagate the exception."""
    cond = {"op": "exists", "system": "flaky2",
            "query": {"type": "x", "where": {}}}
    vex = Vexinter(embedded=True, adapters=[_FlakyLuegoCorte()])
    with vex.task(agent="a", objective="o", condition=cond, input={}) as t:
        t.tools.flaky2.do({"x": 1})                    # ok=False
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="flaky2.do")      # el reintento no responde
        assert case.status == CaseStatus.WAITING_HUMAN
        assert "retry" not in {o.action for o in t.case_options(case.id)}
        assert t.audit().ok


COND_SIN_EMITIDAS = {"op": "count", "system": "billing",
                     "query": {"type": "invoice", "where": {"status": "issued"}},
                     "equals": 0}
COND_HAY_EMITIDA = {"op": "exists", "system": "billing",
                    "query": {"type": "invoice", "where": {"status": "issued"}}}


class _BillingCompensable(BillingAdapter):
    """create_invoice reversible, without idempotency, declaring how to undo it."""
    def tools(self):
        t = super().tools()
        t["create_invoice"] = t["create_invoice"].model_copy(update={
            "operation_class": OperationClass.REVERSIBLE_WRITE,
            "supports_idempotency": False,
            "compensation": ToolCompensation(
                tool="billing.void_invoice",
                args_from_result={"invoice_id": "id"})})
        return t


class _BillingCortaCompensable(_BillingCompensable):
    def invoke(self, tool, req):
        if tool == "create_invoice":
            raise IndeterminateOutcome("sin respuesta (simulado)")
        return super().invoke(tool, req)


def test_compensar_por_human_decision_anula_lo_que_quedo_persistido():
    """§7.3: el efecto ambiguo (error informado, factura persistida) se
    deshace desde el caso, con rastro, y se re-verifica."""
    billing = _BillingCompensable(fail_first=1, leak_on_fail=True)
    vex = Vexinter(embedded=True, adapters=[billing])
    with vex.task(agent="a", objective="o", condition=COND_SIN_EMITIDAS,
                  input={}) as t:
        r = t.tools.billing.create_invoice({"customer_id": "c1", "amount": 100})
        assert not r.ok
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert "compensate" in {o.action for o in t.case_options(case.id)}
        v = t.decide(case.id, "compensate")
        assert v.status == "met"
        assert case.status == CaseStatus.RESOLVED
        assert billing.find({})[0]["status"] == "voided"
        comp = [e for e in t.events()
                if e.type == EventType.RECOVERY_EXECUTED and e.compensates]
        assert len(comp) == 1
        assert comp[0].compensates == r.provenance.invoked_event
        assert t.audit().ok


def test_with_no_recorded_result_compensation_is_not_offered():
    """An invocation without return (cut) has no id to undo."""
    import pytest
    vex = Vexinter(embedded=True, adapters=[_BillingCortaCompensable()])
    with vex.task(agent="a", objective="o", condition=COND_HAY_EMITIDA,
                  input={}) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.billing.create_invoice({"customer_id": "c1", "amount": 100})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert "compensate" not in {o.action for o in t.case_options(case.id)}


def test_a_failed_compensation_leaves_the_case_open_without_edge():
    """The log only asserts what happened (§7.3): if cancelling fails, there
    is no 'compensated' edge and the case keeps waiting for a person."""
    billing = _BillingCompensable(fail_first=1)     # fake success: does not persist
    vex = Vexinter(embedded=True, adapters=[billing])
    with vex.task(agent="a", objective="o", condition=COND_HAY_EMITIDA,
                  input={}) as t:
        t.tools.billing.create_invoice({"customer_id": "c1", "amount": 100})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="billing.create_invoice")
        assert "compensate" in {o.action for o in t.case_options(case.id)}
        t.decide(case.id, "compensate")         
        assert case.status == CaseStatus.WAITING_HUMAN
        assert not any(e.compensates for e in t.events())
        assert t.audit().ok


def test_an_unresolved_reference_is_reported_as_such():
    """An unresolved reference is neither a dead source nor an exception."""
    from vexinter.core.conditions import EvalContext, eval_condition
    from vexinter.core.tools import SourceRouter
    router = SourceRouter([CrmAdapter()])
    donde = {"where": {"id": {"$ref": "cliente.id"}}}
    for op, extra in (("exists", {}), ("count", {"equals": 1}),
                      ("snapshot_changed", {})):
        nodo = {"op": op, "system": "crm",
                "query": {"type": "customer", **donde}, **extra}
        r = eval_condition(nodo, EvalContext({}, {}), router)
        assert r.outcome == "inconclusive"
        assert r.reason == "unresolved_reference"
        assert "cliente.id" in (r.detail or "")


from vexinter.core.tools import AmbiguousInvocationError as _Ambiguo


class _PierdeRespuestaSinIdemTest:
    """No idempotente: el efecto ocurre y la respuesta se pierde (una vez)."""
    system = "sinidem"

    def __init__(self):
        self.efectos = 0

    def tools(self):
        return {"do": ToolSpec(system="sinidem", name="do",
                               operation_class=OperationClass.IDEMPOTENT_WRITE,
                               supports_idempotency=False)}

    def invoke(self, tool, req):
        self.efectos += 1
        if self.efectos == 1:
            raise IndeterminateOutcome("respuesta perdida (simulado)")
        return OperationResult(ok=True, data={"n": self.efectos})

    def observe(self, query):
        return Observation(data=None, observed_at=_dt.now(UTC),
                           source="sinidem")


def _tarea_sinidem(adapter):
    vex = Vexinter(embedded=True, adapters=[adapter])
    return vex.task(agent="a", objective="o", condition=CONDICION,
                    input={"email": DATOS["email"]})


def test_reintento_tras_respuesta_perdida_en_herramienta_no_idempotente_se_bloquea():
    import pytest
    adapter = _PierdeRespuestaSinIdemTest()
    with _tarea_sinidem(adapter) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "sinidem.do", {"x": 1})
        with pytest.raises(_Ambiguo):
            inter.call(ctx, "sinidem.do", {"x": 1})
        assert adapter.efectos == 1                 # did not run again


def test_a_distinct_operation_is_not_blocked():
    import pytest
    adapter = _PierdeRespuestaSinIdemTest()
    with _tarea_sinidem(adapter) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "sinidem.do", {"x": 1})
        assert inter.call(ctx, "sinidem.do", {"x": 2}).ok


def test_the_retry_authorized_by_a_person_does_run():
    import pytest
    adapter = _PierdeRespuestaSinIdemTest()
    with _tarea_sinidem(adapter) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "sinidem.do", {"x": 1})
        assert inter.call(ctx, "sinidem.do", {"x": 1},
                          retry_authorized=True).ok
        assert adapter.efectos == 2


from vexinter.core.tools import EffectAlreadyApplied, ToolProbe


class _Almacen:
    """Escritura no idempotente con un sondeo declarado. Modos:
    pierde_respuesta (efecto + sin respuesta, una vez), corte_antes (sin
    efecto y sin respuesta, una vez), error_con_efecto (efecto + error
    informado, una vez)."""
    system = "alm"

    def __init__(self, modo, lectura="strong"):
        self.modo, self.lectura = modo, lectura
        self.efectos, self.llamadas = [], 0

    def tools(self):
        return {"do": ToolSpec(
            system="alm", name="do",
            operation_class=OperationClass.REVERSIBLE_WRITE,
            supports_idempotency=False,
            effect_probe=ToolProbe(
                system="alm",
                query={"type": "efecto",
                       "where": {"k": {"$ref": "args.k"}}}))}

    def invoke(self, tool, req):
        self.llamadas += 1
        primera = self.llamadas == 1
        if self.modo == "corte_antes" and primera:
            raise IndeterminateOutcome("corte antes del efecto")
        self.efectos.append({"k": req.args["k"]})
        if primera and self.modo == "pierde_respuesta":
            raise IndeterminateOutcome("respuesta perdida")
        if primera and self.modo == "error_con_efecto":
            return OperationResult(ok=False, error="timeout simulado",
                                   data={"k": req.args["k"]})
        return OperationResult(ok=True, data={"k": req.args["k"]})

    def observe(self, query):
        where = query.get("where", {})
        data = [e for e in self.efectos
                if all(e.get(k) == v for k, v in where.items())]
        return Observation(data=data, observed_at=_dt.now(UTC),
                           source="alm", consistency=self.lectura)


def test_respuesta_perdida_con_efecto_comprobado_no_se_repite():
    import pytest
    a = _Almacen("pierde_respuesta")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(EffectAlreadyApplied):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 1 and a.llamadas == 1


def test_corte_antes_del_efecto_se_reintenta_sin_duplicar():
    import pytest
    a = _Almacen("corte_antes")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert inter.call(ctx, "alm.do", {"k": "x"}).ok
        assert len(a.efectos) == 1 and a.llamadas == 2


def test_error_informado_con_efecto_persistido_bloquea_el_reintento():
    import pytest
    a = _Almacen("error_con_efecto")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        assert not inter.call(ctx, "alm.do", {"k": "x"}).ok
        with pytest.raises(EffectAlreadyApplied):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 1
        # una persona puede autorizar el reintento igual
        assert inter.call(ctx, "alm.do", {"k": "x"},
                          retry_authorized=True).ok
        assert len(a.efectos) == 2


def test_without_conclusive_read_the_conservative_block_stays():
    import pytest
    a = _Almacen("pierde_respuesta", lectura="eventual")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(_Ambiguo) as e:
            inter.call(ctx, "alm.do", {"k": "x"})
        assert not isinstance(e.value, EffectAlreadyApplied)
        assert len(a.efectos) == 1


def test_error_registrado_sin_lectura_concluyente_se_reintenta_como_antes():
    """Documented limit: unable to check, an explicit error
    reintenta (y puede duplicar). Se evita declarando un sondeo."""
    a = _Almacen("error_con_efecto", lectura="eventual")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        assert not inter.call(ctx, "alm.do", {"k": "x"}).ok
        assert inter.call(ctx, "alm.do", {"k": "x"}).ok
        assert len(a.efectos) == 2


def test_the_previous_count_is_recorded_in_the_invocation_log():
    a = _Almacen("corte_antes")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        try:
            inter.call(ctx, "alm.do", {"k": "x"})
        except IndeterminateOutcome:
            pass
        inv = next(e for e in t.events() if e.type == EventType.TOOL_INVOKED)
        assert inv.payload["probe_before"]["count"] == 0
        assert inv.payload["probe_before"]["consistency"] == "strong"
        assert t.audit().ok


class _AlmacenReintentable(_Almacen):
    """Como _Almacen, pero de clase IDEMPOTENT_WRITE: the recovery
    policy allows a retry."""
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(
            update={"operation_class": OperationClass.IDEMPOTENT_WRITE})
        return t


COND_ALM_X = {"op": "exists", "system": "alm",
              "query": {"type": "efecto", "where": {"k": "x"}}}
COND_ALM_NUNCA = {"op": "exists", "system": "alm",
                  "query": {"type": "efecto", "where": {"k": "otro"}}}


def _tarea_alm(adapter, cond):
    vex = Vexinter(embedded=True, adapters=[adapter])
    return vex.task(agent="a", objective="o", condition=cond, input={})


def test_recovery_retries_if_the_read_proves_no_effect_happened():
    import pytest
    a = _AlmacenReintentable("corte_antes")
    with _tarea_alm(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.RESOLVED
        assert len(a.efectos) == 1 and a.llamadas == 2
        assert t.audit().ok


def test_recovery_does_not_retry_if_the_read_shows_the_effect_happened():
    a = _AlmacenReintentable("error_con_efecto")
    with _tarea_alm(a, COND_ALM_NUNCA) as t:
        assert not t.tools.alm.do({"k": "x"}).ok     # error informado, efecto persistido
        assert t.verify().status == "not_met"      # the condition was never met
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert len(a.efectos) == 1 and a.llamadas == 1   # it did not retry


def test_without_conclusive_read_it_is_not_checked_whether_the_effect_did_not_happen():
    import pytest

    from vexinter.core.recovery import last_invocation
    a = _AlmacenReintentable("corte_antes", lectura="eventual")
    with _tarea_alm(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        inv = last_invocation(t._rt.store, t._run, "alm.do")
        assert inv["probe_before"]["count"] is None
        assert not t._rt.intermediary.retry_is_provably_safe("alm.do", inv)


class _AlmacenAprobacion(_AlmacenReintentable):
    """Como _AlmacenReintentable, pero requires human approval to repeat."""
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(update={"retry_approval": "human"})
        return t


def test_the_default_approval_value_is_auto():
    spec = ToolSpec(system="s", name="n")
    assert spec.retry_approval == "auto"


def test_with_human_approval_the_agent_retry_is_blocked():
    import pytest

    from vexinter.core.tools import ApprovalRequired
    a = _AlmacenAprobacion("corte_antes")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(ApprovalRequired):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert a.llamadas == 1                      # did not run again


def test_with_human_approval_recovery_does_not_retry_by_itself():
    import pytest
    a = _AlmacenAprobacion("corte_antes")
    with _tarea_alm(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert a.llamadas == 1
        assert "retry" in {o.action for o in t.case_options(case.id)}
        motivos = [e.payload["reason"] for e in t.events()
                   if e.type == EventType.RECOVERY_DECIDED]
        assert any("human approval" in m for m in motivos)


def test_the_person_approves_the_retry_and_it_runs():
    a = _AlmacenAprobacion("corte_antes")
    with _tarea_alm(a, COND_ALM_X) as t:
        try:
            t.tools.alm.do({"k": "x"})
        except IndeterminateOutcome:
            pass
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        v = t.decide(case.id, "retry")
        assert v.status == "met"
        assert case.status == CaseStatus.RESOLVED
        assert a.llamadas == 2 and len(a.efectos) == 1


def test_human_approval_does_not_affect_idempotent_tools():
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(),
                             BillingAdapter(fail_first=1, aprobacion="human")])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status == CaseStatus.RESOLVED
        assert t.verify().status == "met"


def test_if_the_effect_already_happened_retry_is_not_offered_to_the_person():
    a = _AlmacenReintentable("error_con_efecto")
    with _tarea_alm(a, COND_ALM_NUNCA) as t:
        assert not t.tools.alm.do({"k": "x"}).ok     # error, efecto persistido
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        acciones = {o.action for o in t.case_options(case.id)}
        assert "retry" not in acciones and "reverify" in acciones
        motivos = [e.payload["reason"] for e in t.events()
                   if e.type == EventType.RECOVERY_DECIDED]
        assert any("already happened" in m for m in motivos)
        assert len(a.efectos) == 1


from vexinter.core.recovery import DEFAULT_POLICIES


class _AlmacenRevAprobacion(_Almacen):
    """REVERSIBLE write without idempotency requiring human approval."""
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(update={"retry_approval": "human"})
        return t


def test_reversible_policy_allows_a_retry_and_irreversible_does_not():
    rev = DEFAULT_POLICIES[OperationClass.REVERSIBLE_WRITE]
    assert "retry" in rev.allowed and rev.max_retries == 1
    irr = DEFAULT_POLICIES[OperationClass.EFFECTFUL_IRREVERSIBLE]
    assert "retry" not in irr.allowed and irr.max_retries == 0


def test_a_reversible_write_without_idempotency_retries_if_no_effect():
    import pytest
    a = _Almacen("corte_antes")           # REVERSIBLE_WRITE con sondeo
    with _tarea_alm(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.RESOLVED
        assert a.llamadas == 2 and len(a.efectos) == 1
        assert t.audit().ok


def test_a_reversible_write_with_human_approval_is_approved_by_a_person():
    import pytest
    a = _AlmacenRevAprobacion("corte_antes")
    with _tarea_alm(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert a.llamadas == 1                      # it did not retry sola
        assert "retry" in {o.action for o in t.case_options(case.id)}
        motivos = [e.payload["reason"] for e in t.events()
                   if e.type == EventType.RECOVERY_DECIDED]
        assert any("human approval" in m for m in motivos)
        v = t.decide(case.id, "retry")
        assert v.status == "met"
        assert case.status == CaseStatus.RESOLVED
        assert a.llamadas == 2 and len(a.efectos) == 1


def test_a_reversible_write_does_not_retry_if_the_effect_already_happened():
    a = _Almacen("error_con_efecto")
    with _tarea_alm(a, COND_ALM_NUNCA) as t:
        assert not t.tools.alm.do({"k": "x"}).ok     # error, efecto persistido
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert a.llamadas == 1 and len(a.efectos) == 1
        acciones = {o.action for o in t.case_options(case.id)}
        assert "retry" not in acciones and "reverify" in acciones


class _AlmacenSondeoMalo(_Almacen):
    """Su sondeo mide algo que nunca cambia (consulta mal definida)."""
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(update={"effect_probe": ToolProbe(
            system="alm",
            query={"type": "efecto", "where": {"k": "__ninguno__"}})})
        return t


class _AlmacenSondeoMaloConAprobacion(_AlmacenSondeoMalo):
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(update={"retry_approval": "human"})
        return t


COND_ALM_UNO = {"op": "count", "system": "alm",
                "query": {"type": "efecto", "where": {"k": "x"}},
                "equals": 1}


def test_a_defective_probe_allows_duplication_with_auto_approval():
    """Characterization of the limit: if the probe is misdefined, 'auto'
    reintenta a ciegas y duplica el efecto."""
    import pytest
    a = _AlmacenSondeoMalo("pierde_respuesta")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert inter.call(ctx, "alm.do", {"k": "x"}).ok   # el sondeo no vio el efecto
        assert len(a.efectos) == 2                         # and it duplicated


def test_human_approval_protects_even_with_a_defective_probe():
    import pytest

    from vexinter.core.tools import ApprovalRequired
    a = _AlmacenSondeoMaloConAprobacion("pierde_respuesta")
    with _tarea_sinidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(ApprovalRequired):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 1


def test_aun_con_un_sondeo_defectuoso_el_verificador_detecta_el_duplicado():
    """El veredicto sale de las condiciones del verificador, no del sondeo."""
    import pytest
    a = _AlmacenSondeoMalo("pierde_respuesta")
    with _tarea_alm(a, COND_ALM_UNO) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 2
        assert t.verify().status == "not_met"


def test_conformidad_del_sondeo_el_conteo_sube_tras_una_escritura_real():
    """Todo adaptador que declara un sondeo debe cumplir esto: tras una
    successful write raises the count. Otherwise the automatic retry is blind."""
    billing = BillingAdapter(sondeable=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        c = t.tools.crm.create_customer(DATOS)
        inter = t._rt.intermediary
        spec = inter.spec_of("billing.create_invoice")
        args = {"customer_id": c.id, "amount": 100}
        assert inter._probe(spec, args)["count"] == 0
        assert t.tools.billing.create_invoice(args).ok
        assert inter._probe(spec, args)["count"] == 1


def _alta_y_args(t):
    c = t.tools.crm.create_customer(DATOS)
    return {"customer_id": c.id, "amount": 100}


def test_once_per_execution_is_disabled_by_default():
    assert ToolSpec(system="s", name="n").once_per_execution is False
    billing = BillingAdapter()
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        args = _alta_y_args(t)
        t.tools.billing.create_invoice(args)
        t.tools.billing.create_invoice(args)
        assert len(billing.find({})) == 2          # repeating is legitimate


def test_once_per_execution_returns_what_was_recorded_without_executing():
    billing = BillingAdapter(una_vez=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        args = _alta_y_args(t)
        r1 = t.tools.billing.create_invoice(args)
        r2 = t.tools.billing.create_invoice(args)
        assert r1.ok and r2.ok and r1.data["id"] == r2.data["id"]
        assert len(billing.find({})) == 1
        assert r2.provenance.invoked_event == r1.provenance.invoked_event
        replays = [e for e in t.events() if e.type == EventType.TOOL_REPLAYED]
        assert len(replays) == 1
        assert replays[0].payload["reason"] == "once_per_execution"
        invocadas = [e for e in t.events()
                     if e.type == EventType.TOOL_INVOKED
                     and e.payload["tool"] == "billing.create_invoice"]
        assert len(invocadas) == 1
        assert t.audit().ok


def test_a_distinct_operation_is_not_blocked_with_once_per_execution():
    billing = BillingAdapter(una_vez=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        args = _alta_y_args(t)
        t.tools.billing.create_invoice(args)
        t.tools.billing.create_invoice({**args, "amount": 200})
        assert len(billing.find({})) == 2


def test_a_compensated_operation_can_be_repeated():
    billing = BillingAdapter(una_vez=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        args = _alta_y_args(t)
        r1 = t.tools.billing.create_invoice(args)
        t._rt.store.append(EventDraft(
            type=EventType.RECOVERY_EXECUTED, task_id=t.task_id,
            execution_id=t.execution_id,
            compensates=r1.provenance.invoked_event,
            payload={"tool": "billing.void_invoice", "via": "test",
                     "ok": True}), stream=t._run.stream)
        t.tools.billing.create_invoice(args)
        assert len(billing.find({})) == 2


def test_human_authorization_allows_repeating_once_per_execution():
    billing = BillingAdapter(una_vez=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDICION,
                  input={"email": DATOS["email"]}) as t:
        args = _alta_y_args(t)
        t.tools.billing.create_invoice(args)
        inter, ctx = t._rt.intermediary, t._run.call_context()
        assert inter.call(ctx, "billing.create_invoice", args,
                          retry_authorized=True).ok
        assert len(billing.find({})) == 2

def test_derive_ignora_transiciones_invalidas_en_el_log():
    """Un evento state_changed con arista inválida no corrompe la proyección."""
    from vexinter.core.events import EventDraft, EventType
    from vexinter.core.statemachine import ExecutionState, ExecutionTracker
    from vexinter.core.store import InMemoryEventStore
    store = InMemoryEventStore()
    tr = ExecutionTracker(store)
    for to in ("satisfied", "provisionally_satisfied", "failed"):
        store.append(EventDraft(
            type=EventType.EXECUTION_STATE_CHANGED,
            task_id="t1", execution_id="e1",
            payload={"from": "in_progress", "to": to}),
            stream="s1")
    # satisfied -> provisionally_satisfied is not a VALID edge: skipped.
    # satisfied -> failed IS valid, so the last event still applies.
    assert tr.current("e1") == ExecutionState.FAILED


def test_canonical_json_serializa_tipos_deterministas():
    """UUID/Decimal/set/bytes producen una forma canónica estable."""
    import uuid
    from decimal import Decimal

    from vexinter.core.crypto import canonical_json
    a = {"id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
         "amount": Decimal("10.50"), "tags": {"b", "a"},
         "raw": b"\x01\x02"}
    b = {"id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
         "amount": Decimal("10.50"), "tags": {"a", "b"},
         "raw": b"\x01\x02"}
    assert canonical_json(a) == canonical_json(b)
    try:
        canonical_json({"x": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("tipos no serializables deben fallar")


def test_subcondiciones_identicas_no_repiten_la_consulta():
    """Dos nodos exists idénticos comparten una sola llamada al router."""
    from datetime import UTC, datetime

    from vexinter.core.conditions import EvalContext, eval_condition
    from vexinter.core.tools import Observation, SourceRouter
    class CountingAdapter:
        system = "crm"
        def __init__(self):
            self.calls = 0
        def observe(self, query):
            self.calls += 1
            return Observation(data=[{"id": 1}],
                               observed_at=datetime.now(UTC),
                               source="crm", consistency="strong")

    ad = CountingAdapter()
    router = SourceRouter([ad])
    nodo = {"op": "exists", "system": "crm",
            "query": {"type": "customer", "id": 1}}
    cond = {"op": "all", "args": [nodo, dict(nodo)]}
    r = eval_condition(cond, EvalContext({}, {}), router)
    assert r.outcome == "met"
    assert ad.calls == 1


def test_observe_only_retries_transient_errors():
    """Errores permanentes (validación/auth) no se reintentan."""
    from vexinter.core.tools import SourceRouter

    class FailAdapter:
        system = "crm"
        def __init__(self, exc):
            self.exc, self.calls = exc, 0
        def observe(self, query):
            self.calls += 1
            raise self.exc

    permanente = FailAdapter(ValueError("bad query"))
    router = SourceRouter([permanente], retries=3, backoff_s=0)
    try:
        router.observe("crm", {"q": 1})
    except ValueError:
        pass
    assert permanente.calls == 1          # sin retries

    transitorio = FailAdapter(ConnectionError("boom"))
    router2 = SourceRouter([transitorio], retries=2, backoff_s=0)
    try:
        router2.observe("crm", {"q": 1})
    except ConnectionError:
        pass
    assert transitorio.calls == 3         # retries+1
