from datetime import timedelta

from examples.demo_coordinated import (
    DATA,
    coordinated_condition,
    plan_two_steps,
    plan_three_steps,
)
from vexinter import Vexinter
from vexinter.core.events import EventType, now
from vexinter.core.recovery import CaseStatus
from vexinter.core.statemachine import VALID, ExecutionState, derive_execution_state
from vexinter.core.tools import IndeterminateOutcome, OperationResult
from vexinter.demo import BillingSystem, CrmSystem, NotifierSystem


def _vex(*adapters):
    return Vexinter(embedded=True, adapters=list(adapters))


def _task(vex, **kw):
    return vex.task(agent="orchestrator", condition=coordinated_condition(),
                    input={"email": DATA["email"]}, **kw)


def test_the_state_machine_forbids_contradictory_transitions():
    """Avoid contradictory transitions."""
    assert ExecutionState.IN_PROGRESS not in VALID[ExecutionState.FAILED]
    assert ExecutionState.IN_PROGRESS not in VALID[ExecutionState.SATISFIED]
    # but new evidence can arrive from any waiting state:
    assert ExecutionState.SATISFIED in VALID[ExecutionState.PENDING_HUMAN]


def test_forward_resumes_the_plan_after_a_false_success():
    vex = _vex(CrmSystem(), BillingSystem(fail_first=1))
    with _task(vex, objective="o") as t:
        v = t.run_plan(plan_two_steps())
        assert v.status == "met"
        assert t.state is ExecutionState.SATISFIED
        assert t.audit().ok


def test_backward_compensates_without_deleting_history():
    """Restore a valid state without losing the history."""
    crm, billing = CrmSystem(), BillingSystem()
    vex = _vex(crm, billing, NotifierSystem())
    with _task(vex, objective="o") as t:
        v = t.run_plan(plan_three_steps())
        assert v.status == "not_met"
        assert t.state is ExecutionState.FAILED
        # the voided invoice STAYS in the system (audit trail)
        invoices = billing.find({})
        assert invoices and invoices[0]["status"] == "voided"
        # the customer ended up archived, not deleted
        assert crm.observe({"type": "customer",
                            "where": {"email": DATA["email"]}}
                           ).data["status"] == "archived"
        # the graph keeps the «compensated» edge
        assert any(e["rel"] == "compenso_a" for e in t.graph()["edges"])
        # and the explicit dependencies between steps
        assert any(e["rel"] == "depende_de" for e in t.graph()["edges"])
        assert t.audit().ok


def test_effect_ambiguous_breaks_the_invariant_y_escalates_a_human():
    """ +: compensation cannot fake consistency."""
    billing = BillingSystem(fail_first=1, supports_idempotency=False,
                            leak_on_fail=True)
    vex = _vex(CrmSystem(), billing)
    with _task(vex, objective="o") as t:
        t.run_plan(plan_two_steps())
        assert t.state is ExecutionState.PENDING_HUMAN
        cases = t.pending_cases()
        assert len(cases) == 1 and cases[0].status == CaseStatus.WAITING_HUMAN
        # the filtered invoice is observable evidence of the case
        assert billing.find({"status": "issued"})


def test_the_recorded_state_matches_the_projection():
    """The log is the source of truth; memory never
    diverges from the projection."""
    vex = _vex(CrmSystem(), BillingSystem(fail_first=1))
    with _task(vex, objective="o") as t:
        t.run_plan(plan_two_steps())
        assert derive_execution_state(t.events()) is t.state


def test_late_review_reopens_a_closed_execution():
    """A review can generate a new event and
    update the operational state without erasing prior evidence."""
    billing = BillingSystem()
    vex = _vex(CrmSystem(), billing)
    with _task(vex, objective="o") as t:
        t.run_plan(plan_two_steps())
        assert t.state is ExecutionState.SATISFIED
        # an external process voids the invoice after closing
        billing.find({"status": "issued"})[0]["status"] = "voided"
        v = t.verify()
        assert v.status == "not_met"
        assert t.state is ExecutionState.FAILED
        assert t.audit().ok          # the previous evidence stays intact

class _BillingVoidFalla(BillingSystem):
    """Invoice voiding always returns error."""
    def invoke(self, tool, req):
        if tool == "void_invoice":
            return OperationResult(ok=False,
                                   error="voiding rejected (simulated)")
        return super().invoke(tool, req)


def test_failed_compensation_is_not_recorded_as_compensated():
    """ A compensation that fails cannot fake consistency:
    it stops, leaves no 'compensated' edge and escalates to a person."""
    crm, billing = CrmSystem(), _BillingVoidFalla()
    vex = _vex(crm, billing, NotifierSystem())
    with _task(vex, objective="o") as t:
        t.run_plan(plan_three_steps())
        assert t.state is ExecutionState.PENDING_HUMAN
        assert len(t.pending_cases()) == 1
        # the invoice is still issued: nothing voided it
        assert billing.find({"status": "issued"})
        # it stopped: the customer was NOT archived
        assert crm.observe({"type": "customer",
                            "where": {"email": DATA["email"]}}
                           ).data["status"] == "active"
        # no 'compensated' edge: no compensation succeeded
        assert not any(e["rel"] == "compenso_a" for e in t.graph()["edges"])
        assert t.audit().ok

def test_postcondition_not_conclusive_returns_y_records_a_verdict():
    """A lagging replica is neither failure nor success: the plan returns
    'inconclusive' and leaves the verdict as evidence in the log."""
    billing = BillingSystem(read_consistency="eventual")
    vex = _vex(CrmSystem(), billing)
    with _task(vex, objective="o") as t:
        v = t.run_plan(plan_two_steps())
        assert v is not None
        assert v.status == "inconclusive"
        assert t.state is ExecutionState.INCONCLUSIVE
        assert len(billing.find({})) == 1           # nothing was re-executed
        veredictos = [e.payload for e in t.events()
                      if e.type == EventType.VERIFIER_VERDICT]
        assert veredictos[-1]["status"] == "inconclusive"
        assert veredictos[-1]["scope"] == "step_postcondition"
        assert derive_execution_state(t.events()) is t.state
        assert t.audit().ok

def test_inconclusive_plan_schedules_review_and_closes():
    """Plan uncertainty is not left hanging: a later review is
    scheduled and, once the replica catches up, the execution is met."""
    billing = BillingSystem(read_consistency="eventual")
    vex = _vex(CrmSystem(), billing)
    with _task(vex, objective="o") as t:
        v = t.run_plan(plan_two_steps())
        assert v.status == "inconclusive"
        assert any(e.type == EventType.FOLLOWUP_SCHEDULED for e in t.events())
        billing.tick()
        results = t.run_followups(at=now() + timedelta(seconds=61))
        assert results[-1].status == "met"
        assert t.state is ExecutionState.SATISFIED
        assert derive_execution_state(t.events()) is t.state

class _BillingSinRespuesta(BillingSystem):
    """Billing does not give a definitive answer."""
    def invoke(self, tool, req):
        if tool == "create_invoice":
            raise IndeterminateOutcome("no response (simulated)")
        return super().invoke(tool, req)


def test_step_without_response_definitive_escalates_a_human_without_compensar():
    """Unknown whether the effect happened: neither retry nor compensation."""
    crm = CrmSystem()
    vex = _vex(crm, _BillingSinRespuesta())
    with _task(vex, objective="o") as t:
        v = t.run_plan(plan_two_steps())
        assert v is None
        assert t.state is ExecutionState.PENDING_HUMAN
        assert len(t.pending_cases()) == 1
        # the customer was NOT archived: nothing was compensated blindly
        assert crm.observe({"type": "customer",
                            "where": {"email": DATA["email"]}}
                           ).data["status"] == "active"
        # the invocation was recorded, with no return
        invoked = [e for e in t.events()
                     if e.type == EventType.TOOL_INVOKED
                     and e.payload["tool"] == "billing.create_invoice"]
        retornadas = {e.operation_id for e in t.events()
                      if e.type == EventType.TOOL_RETURNED}
        assert len(invoked) == 1
        assert invoked[0].operation_id not in retornadas
        assert t.audit().ok