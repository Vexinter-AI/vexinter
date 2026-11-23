from datetime import UTC, timedelta
from datetime import datetime as _dt

from examples.demo_crm_billing import CONDITION, DATA
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


def test_tampering_of_register_is_detectected():
    """ Modificar un acontecimiento breaks la chain."""
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


def test_false_success_detected_y_recovered_with_idempotency():
    """A fake success is detected and the retry does not duplicate."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(), BillingAdapter(fail_first=1)])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"            
        t.recover(failed_tool="billing.create_invoice")
        assert t.verify().status == "met"
        n = sum(1 for e in t.events()
                if e.type == EventType.TOOL_INVOKED
                and e.payload["tool"] == "billing.create_invoice")
        assert n == 2                                      


def test_replica_stale_produces_not_conclusive_y_the_review_cierra():
    """ +: uncertainty is preserved and resolved by review."""
    billing = BillingAdapter(read_consistency="eventual")
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "inconclusive"       
        billing.tick()
        assert t.run_followups(at=now() + timedelta(seconds=61))[-1].status == "met"


def test_human_decision_reverifies_before_of_execute():
    """ If state changed while waiting, the action is not executed."""
    billing = BillingAdapter(fail_first=1, supports_idempotency=False)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status == CaseStatus.WAITING_HUMAN
        billing.simulate_external_sync({"customer_id": c.id, "amount": 100})
        v = t.decide(case.id, action="retry")
        assert v.status == "met"
        assert case.status == CaseStatus.RESOLVED


def test_conditions_maliciosas_are_rechazadas():
    """ El DSL no permite operations arbitrarias."""
    import pytest

    from vexinter.core.conditions import ConditionError, validate_condition
    with pytest.raises(ConditionError):
        validate_condition({"op": "ejecutar_sql", "query": "DROP TABLE"})
    with pytest.raises(ConditionError):
        validate_condition({"op": "exists"})                

def _keys(t, tool):
    return [e.payload["idempotency_key"] for e in t.events()
            if e.type == EventType.TOOL_INVOKED and e.payload["tool"] == tool]


def test_llamadas_identical_legitimas_have_keys_distintas():
    """ Two identical logical operations must not collapse into one."""
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), BillingAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        keys = _keys(t, "billing.create_invoice")
        assert len(keys) == 2
        assert keys[0] != keys[1]


def test_the_retry_reuses_the_original_key():
    """ The recovery retry keeps the operation key."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(), BillingAdapter(fail_first=1)])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"
        t.recover(failed_tool="billing.create_invoice")
        keys = _keys(t, "billing.create_invoice")
        assert len(keys) == 2
        assert keys[0] == keys[1]

class _FlakyAdapter:
    """Primera call: ok=False (timeout). Las next: ok=True."""
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


def test_call_identical_after_failure_reuses_the_key():
    """ Retrying after ok=False is the SAME logical operation; retrying
    after ok=True is a new operation."""
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        t.tools.flaky.do({"x": 1})     # ok=False
        t.tools.flaky.do({"x": 1})     # same operation: same key
        t.tools.flaky.do({"x": 1})     # the previous one was ok=True: new key
        keys = _keys(t, "flaky.do")
        assert len(keys) == 3
        assert keys[0] == keys[1]
        assert keys[2] != keys[1]

def test_last_invocation_requires_precision_with_distinct_operations():
    """Dos operations distintas de la same tool: no se adivina."""
    import pytest

    from vexinter.core.recovery import last_invocation
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
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
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        t.tools.flaky.do({"x": 1})      # ok=False
        t.tools.flaky.do({"x": 1})      # same key
        inv = last_invocation(t._rt.store, t._run, "flaky.do")
        assert inv["args"] == {"x": 1}

def test_case_ambiguous_not_offers_retry_a_the_person():
    """If the failed operation is unknown, the 'retry' option is not offered."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(), BillingAdapter(), _FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        t.tools.flaky.do({"x": 1})
        t.tools.flaky.do({"x": 2})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="flaky.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        acciones = {o.action for o in t.case_options(case.id)}
        assert "retry" not in acciones
        assert "reverify" in acciones

def test_count_of_retries_is_by_operation_logica():
    """Dos steps con la same tool no comparten el contador de
    retries: se cuenta por key de idempotency."""
    vex = Vexinter(embedded=True, adapters=[_FlakyAdapter()])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        r1 = t.tools.flaky.do({"x": 1})    # step s1: ok=False
        t.tools.flaky.do({"x": 1})         # retry de s1 (same key)
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
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        sch = t._rt.scheduler
        sch.schedule(t._run, 30, "first")
        sch.schedule(t._run, 30, "second")
        agendados = [e for e in t.events()
                     if e.type == EventType.FOLLOWUP_SCHEDULED]
        assert len(agendados) == 1


class _SeCaeAdapter:
    """The first call 'dies' midway (process crashes): no return."""
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


def test_invocation_prior_without_result_not_se_repeats_si_not_is_idempotent():
    """Unknown whether the effect happened: no guessing, escalate."""
    import pytest

    from vexinter.core.tools import AmbiguousInvocationError
    adapter = _SeCaeAdapter()
    vex = Vexinter(embedded=True, adapters=[adapter])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(RuntimeError):
            inter.call(ctx, "boom.do", {"x": 1}, client_key="k1")
        with pytest.raises(AmbiguousInvocationError):
            inter.call(ctx, "boom.do", {"x": 1}, client_key="k1")
        assert adapter.calls == 1
        assert inter.call(ctx, "boom.do", {"x": 1}, client_key="k2").ok


def test_replay_only_when_the_result_recorded_was_ok():
    """ok=False: the client retries on purpose and it runs; ok=True:
    the recorded result is returned without calling the adapter again."""
    adapter = _FlakyAdapter()
    vex = Vexinter(embedded=True, adapters=[adapter])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        r1 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")
        assert not r1.ok
        r2 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")
        assert r2.ok and adapter.calls == 2
        r3 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")
        assert r3.ok and adapter.calls == 2          # replay: did not call
        assert r3.provenance.invoked_event == r2.provenance.invoked_event


def test_the_replay_leaves_a_trace_in_the_log_and_the_graph():
    """ Devolver un result ya recorded es un done auditable."""
    adapter = _FlakyAdapter()
    vex = Vexinter(embedded=True, adapters=[adapter])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")        # ok=False
        r2 = inter.call(ctx, "flaky.do", {"x": 1}, client_key="k")   # ejecuta
        assert inter.call(ctx, "flaky.do", {"x": 1}, client_key="k").ok  # replay
        replays = [e for e in t.events() if e.type == EventType.TOOL_REPLAYED]
        assert len(replays) == 1
        rp = replays[0]
        assert rp.caused_by == r2.provenance.invoked_event
        assert rp.payload["returned_event"] == r2.provenance.returned_event
        assert "data" not in rp.payload          # no duplica content
        assert any(e["rel"] == "se_origino_en"
                   and e["source"] == r2.provenance.invoked_event
                   and e["target"] == rp.id
                   for e in t.graph()["edges"])
        assert t.audit().ok


def test_an_adapter_that_cannot_observe_never_produces_not_met():
    """ With no possible observation the answer is 'inconclusive'.
    Before, 'not exists' on the notifier gave an unfounded 'met'."""
    from vexinter.demo import NotifierSystem
    existe = {"op": "exists", "system": "notify",
              "query": {"type": "email",
                        "where": {"to": {"$ref": "input.email"}}}}
    for cond in (existe, {"op": "not", "arg": existe}):
        vex = Vexinter(embedded=True, adapters=[NotifierSystem(fail=False)])
        with vex.task(agent="a", objective="o", condition=cond,
                      input={"email": DATA["email"]}) as t:
            assert t.verify().status == "inconclusive"


def test_the_verdict_records_source_and_consistency_of_each_read():
    """ Evidence stays in the register, without the observed content."""
    vex = Vexinter(embedded=True,
                   adapters=[CrmAdapter(),
                             BillingAdapter(read_consistency="eventual")])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
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
    """ Comparar la response de la tool consigo same no
    verifica nothing."""
    field = {"op": "field_equals", "from": "steps",
             "path": "a2.data.status", "equals": "issued"}
    vex = Vexinter(embedded=True, adapters=[CrmAdapter()])
    with vex.task(agent="a", objective="o", condition=field, input={}) as t:
        v = t._rt.verifier.verify(
            t._run, extra_bindings={"steps": {"a2": {"data": {"status": "issued"}}}})
        assert v.status == "inconclusive"


def test_reserved_names_cannot_be_used_as_bindings():
    import pytest

    from vexinter.core.conditions import ConditionError, validate_condition
    for name in ("input", "steps"):
        with pytest.raises(ConditionError):
            validate_condition({"op": "exists", "system": "crm",
                                "query": {}, "as": name})

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
        raise IndeterminateOutcome("no response (simulated)")


def test_a_retry_without_answer_escalates_instead_of_breaking():
    """ Si el retry no obtiene response definitive, se desconoce
    whether the effect happened: it escalates to a person, it does not propagate the exception."""
    cond = {"op": "exists", "system": "flaky2",
            "query": {"type": "x", "where": {}}}
    vex = Vexinter(embedded=True, adapters=[_FlakyLuegoCorte()])
    with vex.task(agent="a", objective="o", condition=cond, input={}) as t:
        t.tools.flaky2.do({"x": 1})                    # ok=False
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="flaky2.do")      # el retry no responde
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
            raise IndeterminateOutcome("no response (simulated)")
        return super().invoke(tool, req)


def test_compensate_by_human_decision_voids_what_remained_persisted():
    """ The ambiguous effect (error reported, invoice persisted) is
    undone from the case, with trace, and re-verified."""
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


def test_with_not_recorded_result_compensation_is_not_offered():
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
    """The log only asserts what happened: if cancelling fails, there
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
    where = {"where": {"id": {"$ref": "customer.id"}}}
    for op, extra in (("exists", {}), ("count", {"equals": 1}),
                      ("snapshot_changed", {})):
        node = {"op": op, "system": "crm",
                "query": {"type": "customer", **where}, **extra}
        r = eval_condition(node, EvalContext({}, {}), router)
        assert r.outcome == "inconclusive"
        assert r.reason == "unresolved_reference"
        assert "customer.id" in (r.detail or "")


from vexinter.core.tools import AmbiguousInvocationError as _Ambiguo


class _PierdeRespuestaSinIdemTest:
    """No idempotent: el effect ocurre y la response se pierde (una vez)."""
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
            raise IndeterminateOutcome("response lost (simulado)")
        return OperationResult(ok=True, data={"n": self.efectos})

    def observe(self, query):
        return Observation(data=None, observed_at=_dt.now(UTC),
                           source="sinidem")


def _task_noidem(adapter):
    vex = Vexinter(embedded=True, adapters=[adapter])
    return vex.task(agent="a", objective="o", condition=CONDITION,
                    input={"email": DATA["email"]})


def test_retry_after_response_lost_in_tool_not_idempotent_se_blocks():
    import pytest
    adapter = _PierdeRespuestaSinIdemTest()
    with _task_noidem(adapter) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "sinidem.do", {"x": 1})
        with pytest.raises(_Ambiguo):
            inter.call(ctx, "sinidem.do", {"x": 1})
        assert adapter.efectos == 1                 # did not run again


def test_a_distinct_operation_is_not_blocked():
    import pytest
    adapter = _PierdeRespuestaSinIdemTest()
    with _task_noidem(adapter) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "sinidem.do", {"x": 1})
        assert inter.call(ctx, "sinidem.do", {"x": 2}).ok


def test_the_retry_authorized_by_a_person_does_run():
    import pytest
    adapter = _PierdeRespuestaSinIdemTest()
    with _task_noidem(adapter) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "sinidem.do", {"x": 1})
        assert inter.call(ctx, "sinidem.do", {"x": 1},
                          retry_authorized=True).ok
        assert adapter.efectos == 2


from vexinter.core.tools import EffectAlreadyApplied, ToolProbe


class _Almacen:
    """Non-idempotent write with a declared probe. Modes:
    lost_response (effect + no response, once), cut_before (no
    effect and no response, once), error_with_effect (effect + error
    reported, once)."""
    system = "alm"

    def __init__(self, mode, read="strong"):
        self.mode, self.read = mode, read
        self.efectos, self.llamadas = [], 0

    def tools(self):
        return {"do": ToolSpec(
            system="alm", name="do",
            operation_class=OperationClass.REVERSIBLE_WRITE,
            supports_idempotency=False,
            effect_probe=ToolProbe(
                system="alm",
                query={"type": "effect",
                       "where": {"k": {"$ref": "args.k"}}}))}

    def invoke(self, tool, req):
        self.llamadas += 1
        first = self.llamadas == 1
        if self.mode == "cut_before" and first:
            raise IndeterminateOutcome("corte before del effect")
        self.efectos.append({"k": req.args["k"]})
        if first and self.mode == "lost_response":
            raise IndeterminateOutcome("response lost")
        if first and self.mode == "error_with_effect":
            return OperationResult(ok=False, error="timeout simulado",
                                   data={"k": req.args["k"]})
        return OperationResult(ok=True, data={"k": req.args["k"]})

    def observe(self, query):
        where = query.get("where", {})
        data = [e for e in self.efectos
                if all(e.get(k) == v for k, v in where.items())]
        return Observation(data=data, observed_at=_dt.now(UTC),
                           source="alm", consistency=self.read)


def test_response_lost_with_effect_checked_not_se_repeats():
    import pytest
    a = _Almacen("lost_response")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(EffectAlreadyApplied):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 1 and a.llamadas == 1


def test_corte_before_of_effect_se_retries_without_duplicar():
    import pytest
    a = _Almacen("cut_before")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert inter.call(ctx, "alm.do", {"k": "x"}).ok
        assert len(a.efectos) == 1 and a.llamadas == 2


def test_error_reported_with_effect_persisted_blocks_the_retry():
    import pytest
    a = _Almacen("error_with_effect")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        assert not inter.call(ctx, "alm.do", {"k": "x"}).ok
        with pytest.raises(EffectAlreadyApplied):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 1
        # a person can authorize the retry anyway
        assert inter.call(ctx, "alm.do", {"k": "x"},
                          retry_authorized=True).ok
        assert len(a.efectos) == 2


def test_without_conclusive_read_the_conservative_block_stays():
    import pytest
    a = _Almacen("lost_response", read="eventual")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(_Ambiguo) as e:
            inter.call(ctx, "alm.do", {"k": "x"})
        assert not isinstance(e.value, EffectAlreadyApplied)
        assert len(a.efectos) == 1


def test_error_recorded_without_read_conclusive_se_retries_as_before():
    """Documented limit: unable to check, an explicit error
    retries (and may duplicate). This is avoided by declaring a probe."""
    a = _Almacen("error_with_effect", read="eventual")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        assert not inter.call(ctx, "alm.do", {"k": "x"}).ok
        assert inter.call(ctx, "alm.do", {"k": "x"}).ok
        assert len(a.efectos) == 2


def test_the_previous_count_is_recorded_in_the_invocation_log():
    a = _Almacen("cut_before")
    with _task_noidem(a) as t:
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
    """Like _Almacen, but IDEMPOTENT_WRITE class: the recovery
    policy allows a retry."""
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(
            update={"operation_class": OperationClass.IDEMPOTENT_WRITE})
        return t


COND_ALM_X = {"op": "exists", "system": "alm",
              "query": {"type": "effect", "where": {"k": "x"}}}
COND_ALM_NUNCA = {"op": "exists", "system": "alm",
                  "query": {"type": "effect", "where": {"k": "other"}}}


def _task_vault(adapter, cond):
    vex = Vexinter(embedded=True, adapters=[adapter])
    return vex.task(agent="a", objective="o", condition=cond, input={})


def test_recovery_retries_if_the_read_proves_not_effect_happened():
    import pytest
    a = _AlmacenReintentable("cut_before")
    with _task_vault(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.RESOLVED
        assert len(a.efectos) == 1 and a.llamadas == 2
        assert t.audit().ok


def test_recovery_does_not_retry_if_the_read_shows_the_effect_happened():
    a = _AlmacenReintentable("error_with_effect")
    with _task_vault(a, COND_ALM_NUNCA) as t:
        assert not t.tools.alm.do({"k": "x"}).ok     # error reported, effect persisted
        assert t.verify().status == "not_met"      # the condition was never met
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert len(a.efectos) == 1 and a.llamadas == 1   # it did not retry


def test_without_conclusive_read_it_is_not_checked_whether_the_effect_did_not_happen():
    import pytest

    from vexinter.core.recovery import last_invocation
    a = _AlmacenReintentable("cut_before", read="eventual")
    with _task_vault(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        inv = last_invocation(t._rt.store, t._run, "alm.do")
        assert inv["probe_before"]["count"] is None
        assert not t._rt.intermediary.retry_is_provably_safe("alm.do", inv)


class _AlmacenAprobacion(_AlmacenReintentable):
    """Like _AlmacenReintentable, but requires human approval to repeat."""
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
    a = _AlmacenAprobacion("cut_before")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(ApprovalRequired):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert a.llamadas == 1                      # did not run again


def test_with_human_approval_recovery_does_not_retry_by_itself():
    import pytest
    a = _AlmacenAprobacion("cut_before")
    with _task_vault(a, COND_ALM_X) as t:
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
    a = _AlmacenAprobacion("cut_before")
    with _task_vault(a, COND_ALM_X) as t:
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
                             BillingAdapter(fail_first=1, approval="human")])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="billing.create_invoice")
        assert case.status == CaseStatus.RESOLVED
        assert t.verify().status == "met"


def test_if_the_effect_already_happened_retry_is_not_offered_to_the_person():
    a = _AlmacenReintentable("error_with_effect")
    with _task_vault(a, COND_ALM_NUNCA) as t:
        assert not t.tools.alm.do({"k": "x"}).ok     # error, effect persisted
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


def test_a_reversible_write_without_idempotency_retries_if_not_effect():
    import pytest
    a = _Almacen("cut_before")           # REVERSIBLE_WRITE con probe
    with _task_vault(a, COND_ALM_X) as t:
        with pytest.raises(IndeterminateOutcome):
            t.tools.alm.do({"k": "x"})
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.RESOLVED
        assert a.llamadas == 2 and len(a.efectos) == 1
        assert t.audit().ok


def test_a_reversible_write_with_human_approval_is_approved_by_a_person():
    import pytest
    a = _AlmacenRevAprobacion("cut_before")
    with _task_vault(a, COND_ALM_X) as t:
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
    a = _Almacen("error_with_effect")
    with _task_vault(a, COND_ALM_NUNCA) as t:
        assert not t.tools.alm.do({"k": "x"}).ok     # error, effect persisted
        assert t.verify().status == "not_met"
        case = t.recover(failed_tool="alm.do")
        assert case.status == CaseStatus.WAITING_HUMAN
        assert a.llamadas == 1 and len(a.efectos) == 1
        acciones = {o.action for o in t.case_options(case.id)}
        assert "retry" not in acciones and "reverify" in acciones


class _AlmacenSondeoMalo(_Almacen):
    """Its probe measures something that never changes (badly defined query)."""
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(update={"effect_probe": ToolProbe(
            system="alm",
            query={"type": "effect", "where": {"k": "__ninguno__"}})})
        return t


class _AlmacenSondeoMaloConAprobacion(_AlmacenSondeoMalo):
    def tools(self):
        t = super().tools()
        t["do"] = t["do"].model_copy(update={"retry_approval": "human"})
        return t


COND_ALM_UNO = {"op": "count", "system": "alm",
                "query": {"type": "effect", "where": {"k": "x"}},
                "equals": 1}


def test_a_defective_probe_allows_duplication_with_auto_approval():
    """Characterization of the limit: if the probe is misdefined, 'auto'
    retries a ciegas y duplica el effect."""
    import pytest
    a = _AlmacenSondeoMalo("lost_response")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert inter.call(ctx, "alm.do", {"k": "x"}).ok   # el probe no vio el effect
        assert len(a.efectos) == 2                         # and it duplicated


def test_human_approval_protects_even_with_a_defective_probe():
    import pytest

    from vexinter.core.tools import ApprovalRequired
    a = _AlmacenSondeoMaloConAprobacion("lost_response")
    with _task_noidem(a) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        with pytest.raises(ApprovalRequired):
            inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 1


def test_still_with_a_probe_defective_the_verifier_detects_the_duplicate():
    """El verdict leaves de las conditions del verifier, no del probe."""
    import pytest
    a = _AlmacenSondeoMalo("lost_response")
    with _task_vault(a, COND_ALM_UNO) as t:
        inter, ctx = t._rt.intermediary, t._run.call_context()
        with pytest.raises(IndeterminateOutcome):
            inter.call(ctx, "alm.do", {"k": "x"})
        inter.call(ctx, "alm.do", {"k": "x"})
        assert len(a.efectos) == 2
        assert t.verify().status == "not_met"


def test_conformance_of_probe_the_count_goes_up_after_a_write_real():
    """Every adapter that declares a probe must uphold this: after a
    successful write the count rises. Otherwise the automatic retry is blind."""
    billing = BillingAdapter(probeable=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        c = t.tools.crm.create_customer(DATA)
        inter = t._rt.intermediary
        spec = inter.spec_of("billing.create_invoice")
        args = {"customer_id": c.id, "amount": 100}
        assert inter._probe(spec, args)["count"] == 0
        assert t.tools.billing.create_invoice(args).ok
        assert inter._probe(spec, args)["count"] == 1


def _high_and_args(t):
    c = t.tools.crm.create_customer(DATA)
    return {"customer_id": c.id, "amount": 100}


def test_once_per_execution_is_disabled_by_default():
    assert ToolSpec(system="s", name="n").once_per_execution is False
    billing = BillingAdapter()
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        args = _high_and_args(t)
        t.tools.billing.create_invoice(args)
        t.tools.billing.create_invoice(args)
        assert len(billing.find({})) == 2          # repeating is legitimate


def test_once_per_execution_returns_what_was_recorded_without_executing():
    billing = BillingAdapter(once=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        args = _high_and_args(t)
        r1 = t.tools.billing.create_invoice(args)
        r2 = t.tools.billing.create_invoice(args)
        assert r1.ok and r2.ok and r1.data["id"] == r2.data["id"]
        assert len(billing.find({})) == 1
        assert r2.provenance.invoked_event == r1.provenance.invoked_event
        replays = [e for e in t.events() if e.type == EventType.TOOL_REPLAYED]
        assert len(replays) == 1
        assert replays[0].payload["reason"] == "once_per_execution"
        invoked = [e for e in t.events()
                     if e.type == EventType.TOOL_INVOKED
                     and e.payload["tool"] == "billing.create_invoice"]
        assert len(invoked) == 1
        assert t.audit().ok


def test_a_distinct_operation_is_not_blocked_with_once_per_execution():
    billing = BillingAdapter(once=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        args = _high_and_args(t)
        t.tools.billing.create_invoice(args)
        t.tools.billing.create_invoice({**args, "amount": 200})
        assert len(billing.find({})) == 2


def test_a_compensated_operation_can_be_repeated():
    billing = BillingAdapter(once=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        args = _high_and_args(t)
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
    billing = BillingAdapter(once=True)
    vex = Vexinter(embedded=True, adapters=[CrmAdapter(), billing])
    with vex.task(agent="a", objective="o", condition=CONDITION,
                  input={"email": DATA["email"]}) as t:
        args = _high_and_args(t)
        t.tools.billing.create_invoice(args)
        inter, ctx = t._rt.intermediary, t._run.call_context()
        assert inter.call(ctx, "billing.create_invoice", args,
                          retry_authorized=True).ok
        assert len(billing.find({})) == 2

def test_derive_ignores_invalid_transitions_in_log():
    """A state_changed event with an invalid edge does not corrupt the projection."""
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


def test_canonical_json_serializes_types_deterministas():
    """UUID/Decimal/set/bytes produce a stable canonical shape."""
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
        raise AssertionError("types no serializables deben fallar")


def test_subconditions_identical_not_repeat_the_query():
    """Two identical exists nodes share a single router call."""
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
    node = {"op": "exists", "system": "crm",
            "query": {"type": "customer", "id": 1}}
    cond = {"op": "all", "args": [node, dict(node)]}
    r = eval_condition(cond, EvalContext({}, {}), router)
    assert r.outcome == "met"
    assert ad.calls == 1


def test_observe_only_retries_transient_errors():
    """Permanent errors (validation/auth) are not retried."""
    from vexinter.core.tools import SourceRouter

    class FailAdapter:
        system = "crm"
        def __init__(self, exc):
            self.exc, self.calls = exc, 0
        def observe(self, query):
            self.calls += 1
            raise self.exc

    permanent = FailAdapter(ValueError("bad query"))
    router = SourceRouter([permanent], retries=3, backoff_s=0)
    try:
        router.observe("crm", {"q": 1})
    except ValueError:
        pass
    assert permanent.calls == 1          # no retries

    transitorio = FailAdapter(ConnectionError("boom"))
    router2 = SourceRouter([transitorio], retries=2, backoff_s=0)
    try:
        router2.observe("crm", {"q": 1})
    except ConnectionError:
        pass
    assert transitorio.calls == 3         # retries+1
