from datetime import timedelta

from examples.demo_coordinado import (
    DATOS,
    condicion_coordinada,
    plan_2pasos,
    plan_3pasos,
)
from vexinter import Vexinter
from vexinter.core.events import EventType, now
from vexinter.core.recovery import CaseStatus
from vexinter.core.statemachine import VALID, ExecutionState, derive_execution_state
from vexinter.core.tools import IndeterminateOutcome, OperationResult
from vexinter.demo import BillingSystem, CrmSystem, NotifierSystem


def _vex(*adapters):
    return Vexinter(embedded=True, adapters=list(adapters))


def _tarea(vex, **kw):
    return vex.task(agent="orquestador", condition=condicion_coordinada(),
                    input={"email": DATOS["email"]}, **kw)


def test_the_state_machine_forbids_contradictory_transitions():
    """§13.1: 'evitar transiciones contradictorias'."""
    assert ExecutionState.IN_PROGRESS not in VALID[ExecutionState.FAILED]
    assert ExecutionState.IN_PROGRESS not in VALID[ExecutionState.SATISFIED]
    # pero la evidencia nueva puede llegar desde cualquier estado de espera:
    assert ExecutionState.SATISFIED in VALID[ExecutionState.PENDING_HUMAN]


def test_forward_reanuda_el_plan_tras_un_falso_exito():
    vex = _vex(CrmSystem(), BillingSystem(fail_first=1))
    with _tarea(vex, objective="o") as t:
        v = t.run_plan(plan_2pasos())
        assert v.status == "met"
        assert t.state is ExecutionState.SATISFIED
        assert t.audit().ok


def test_backward_compensa_sin_borrar_historia():
    """§7.3: 'restore a valid state without losing the history'."""
    crm, billing = CrmSystem(), BillingSystem()
    vex = _vex(crm, billing, NotifierSystem())
    with _tarea(vex, objective="o") as t:
        v = t.run_plan(plan_3pasos())
        assert v.status == "not_met"
        assert t.state is ExecutionState.FAILED
        # la factura anulada SIGUE en el sistema (rastro contable, §7.3)
        facturas = billing.find({})
        assert facturas and facturas[0]["status"] == "voided"
        # the customer ended up archived, not deleted
        assert crm.observe({"type": "customer",
                            "where": {"email": DATOS["email"]}}
                           ).data["status"] == "archived"
        # the graph keeps the «compensated» edge (§3.4)
        assert any(e["rel"] == "compenso_a" for e in t.graph()["edges"])
        # and the explicit dependencies between steps (§7.1)
        assert any(e["rel"] == "depende_de" for e in t.graph()["edges"])
        assert t.audit().ok


def test_efecto_ambiguo_rompe_el_invariant_y_deriva_a_humano():
    """§7.2 + §7.3: compensation cannot fake consistency."""
    billing = BillingSystem(fail_first=1, supports_idempotency=False,
                            leak_on_fail=True)
    vex = _vex(CrmSystem(), billing)
    with _tarea(vex, objective="o") as t:
        t.run_plan(plan_2pasos())
        assert t.state is ExecutionState.PENDING_HUMAN
        casos = t.pending_cases()
        assert len(casos) == 1 and casos[0].status == CaseStatus.WAITING_HUMAN
        # la factura filtrada es evidencia observable del caso
        assert billing.find({"status": "issued"})


def test_the_recorded_state_matches_the_projection():
    """§13.1 + §15: el registro es la fuente de verdad; la memoria nunca
    diverges from the projection."""
    vex = _vex(CrmSystem(), BillingSystem(fail_first=1))
    with _tarea(vex, objective="o") as t:
        t.run_plan(plan_2pasos())
        assert derive_execution_state(t.events()) is t.state


def test_revision_tardia_reabre_una_ejecucion_cerrada():
    """§10, step 12: 'a review can generate a new event and
    actualizar el estado operativo sin borrar la evidencia anterior'."""
    billing = BillingSystem()
    vex = _vex(CrmSystem(), billing)
    with _tarea(vex, objective="o") as t:
        t.run_plan(plan_2pasos())
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


def test_compensacion_fallida_no_se_registra_como_compensada():
    """§7.3: a compensation that fails cannot fake consistency:
    it stops, leaves no 'compensated' edge and escalates to a person."""
    crm, billing = CrmSystem(), _BillingVoidFalla()
    vex = _vex(crm, billing, NotifierSystem())
    with _tarea(vex, objective="o") as t:
        t.run_plan(plan_3pasos())
        assert t.state is ExecutionState.PENDING_HUMAN
        assert len(t.pending_cases()) == 1
        # the invoice is still issued: nothing voided it
        assert billing.find({"status": "issued"})
        # it stopped: the customer was NOT archived
        assert crm.observe({"type": "customer",
                            "where": {"email": DATOS["email"]}}
                           ).data["status"] == "active"
        # no 'compensated' edge: no compensation succeeded
        assert not any(e["rel"] == "compenso_a" for e in t.graph()["edges"])
        assert t.audit().ok

def test_postcondicion_no_concluyente_devuelve_y_registra_un_veredicto():
    """§4.3: a lagging replica is neither failure nor success: the plan returns
    'inconclusive' y deja el veredicto como evidencia en el registro."""
    billing = BillingSystem(read_consistency="eventual")
    vex = _vex(CrmSystem(), billing)
    with _tarea(vex, objective="o") as t:
        v = t.run_plan(plan_2pasos())
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

def test_plan_no_concluyente_agenda_revision_y_se_cierra():
    """§4.4: la incertidumbre de un plan no queda abandonada: se agenda una
    later review and, once the replica catches up, the execution is met."""
    billing = BillingSystem(read_consistency="eventual")
    vex = _vex(CrmSystem(), billing)
    with _tarea(vex, objective="o") as t:
        v = t.run_plan(plan_2pasos())
        assert v.status == "inconclusive"
        assert any(e.type == EventType.FOLLOWUP_SCHEDULED for e in t.events())
        billing.tick()
        resultados = t.run_followups(at=now() + timedelta(seconds=61))
        assert resultados[-1].status == "met"
        assert t.state is ExecutionState.SATISFIED
        assert derive_execution_state(t.events()) is t.state

class _BillingSinRespuesta(BillingSystem):
    """Billing does not give a definitive answer."""
    def invoke(self, tool, req):
        if tool == "create_invoice":
            raise IndeterminateOutcome("sin respuesta (simulado)")
        return super().invoke(tool, req)


def test_paso_sin_respuesta_definitiva_deriva_a_humano_sin_compensar():
    """§7.2: unknown whether the effect happened: neither retry nor compensation."""
    crm = CrmSystem()
    vex = _vex(crm, _BillingSinRespuesta())
    with _tarea(vex, objective="o") as t:
        v = t.run_plan(plan_2pasos())
        assert v is None
        assert t.state is ExecutionState.PENDING_HUMAN
        assert len(t.pending_cases()) == 1
        # the customer was NOT archived: nothing was compensated blindly
        assert crm.observe({"type": "customer",
                            "where": {"email": DATOS["email"]}}
                           ).data["status"] == "active"
        # the invocation was recorded, with no return
        invocadas = [e for e in t.events()
                     if e.type == EventType.TOOL_INVOKED
                     and e.payload["tool"] == "billing.create_invoice"]
        retornadas = {e.operation_id for e in t.events()
                      if e.type == EventType.TOOL_RETURNED}
        assert len(invocadas) == 1
        assert invocadas[0].operation_id not in retornadas
        assert t.audit().ok