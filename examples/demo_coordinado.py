"""Partial failures in multi-system flows (§7): forward retry,
backward compensation without deleting history, and ambiguous effect → human."""
from vexinter import Vexinter
from vexinter.core.coordinator import CompensatingSpec, Plan, Step
from vexinter.demo import BillingSystem, CrmSystem, NotifierSystem

DATOS = {"name": "Ana", "email": "ana@ejemplo.com"}


def condicion_coordinada():
    """The global condition expresses the BUSINESS objective (§4.2):
    active customer + ISSUED invoice + correct relationship + no duplicates."""
    return {
        "op": "all",
        "args": [
            {"op": "exists", "system": "crm",
             "query": {"type": "customer",
                       "where": {"email": {"$ref": "input.email"}}},
             "as": "cliente"},
            {"op": "exists", "system": "billing",
             "query": {"type": "invoice",
                       "where": {"customer_id": {"$ref": "cliente.id"},
                                 "status": "issued"}},
             "as": "factura"},
            {"op": "var_equals",
             "a": {"from": "factura", "path": "customer_id"},
             "b": {"from": "cliente", "path": "id"}},          # §7.1
            {"op": "count", "system": "billing",
             "query": {"type": "invoice",
                       "where": {"customer_id": {"$ref": "cliente.id"},
                                 "status": "issued"}},
             "equals": 1},
        ],
    }


def post_recuperacion():
    """Post-recovery invariant (§7.3): after compensating, no ISSUED
    invoice may remain for the execution's customer."""
    return {
        "op": "count", "system": "billing",
        "query": {"type": "invoice",
                  "where": {"customer_id": {"$ref": "steps.a1.data.id"},
                            "status": "issued"}},
        "equals": 0,
    }


def _paso_cliente():
    return Step(
        id="a1", tool="crm.create_customer", args=DATOS,
        postcondition={"op": "exists", "system": "crm",
                       "query": {"type": "customer",
                                 "where": {"email": {"$ref": "input.email"}}},
                       "as": "cliente"},
        compensation=CompensatingSpec(tool="crm.archive_customer",
                                     args_from_result={"customer_id": "id"}))


def _paso_factura():
    return Step(
        id="a2", tool="billing.create_invoice", args={"amount": 100},
        args_from={"customer_id": "a1.data.id"}, depends_on=["a1"],
        postcondition={"op": "exists", "system": "billing",
                      "query": {"type": "invoice",
                                "where": {"customer_id": {"$ref": "cliente.id"},
                                          "status": "issued"}},
                      "as": "factura"},
        compensation=CompensatingSpec(tool="billing.void_invoice",
                                      args_from_result={"invoice_id": "id"}))


def plan_2pasos() -> Plan:
    return Plan(steps=[_paso_cliente(), _paso_factura()],
                post_recovery_condition=post_recuperacion())


def plan_3pasos() -> Plan:
    return Plan(steps=[_paso_cliente(), _paso_factura(),
                       Step(id="a3", tool="notify.send_welcome_email",
                            args_from={"customer_id": "a1.data.id",
                                       "email": "a1.data.email"},
                            depends_on=["a2"])],
                post_recovery_condition=post_recuperacion())


def _vex(*adapters):
    return Vexinter(embedded=True, adapters=list(adapters))


def escenario_4():
    print("── Scenario 4: false success in a step → FORWARD retry (§5.2, §7.3) ──")
    vex = _vex(CrmSystem(), BillingSystem(fail_first=1))
    with vex.task(agent="orquestador",
                  objective="Customer signup and invoice (coordinated plan)",
                  condition=condicion_coordinada(),
                  input={"email": DATOS["email"]}) as t:
        v = t.run_plan(plan_2pasos())
        print("  verdict:", v.status)
        print("  state:   ", t.state.value)
        print("  chain intact:", t.audit().ok)


def escenario_5():
    print("── Scenario 5: later failure → BACKWARD compensation (§7.3) ──")
    crm, billing = CrmSystem(), BillingSystem()
    vex = _vex(crm, billing, NotifierSystem())
    with vex.task(agent="orquestador",
                  objective="Signup, invoice and welcome (coordinated plan)",
                  condition=condicion_coordinada(),
                  input={"email": DATOS["email"]}) as t:
        v = t.run_plan(plan_3pasos())
        print("  verdict:", v.status)
        print("  state:   ", t.state.value)
        print("  invoice voided (not deleted):",
              billing.find({})[0]["status"])
        print("  customer archived (not deleted):",
              crm.observe({"type": "customer",
                           "where": {"email": DATOS["email"]}}).data["status"])
        g = t.graph()
        print("  'compenso_a' edges:",
              [e for e in g["edges"] if e["rel"] == "compenso_a"])
        print("  chain intact:", t.audit().ok)


def escenario_6():
    print("── Scenario 6: ambiguous effect → broken invariant → HUMAN (§7.2, §5.4) ──")
    billing = BillingSystem(fail_first=1, supports_idempotency=False,
                            leak_on_fail=True)
    vex = _vex(CrmSystem(), billing)
    with vex.task(agent="orquestador",
                  objective="Customer signup and invoice (coordinated plan)",
                  condition=condicion_coordinada(),
                  input={"email": DATOS["email"]}) as t:
        v = t.run_plan(plan_2pasos())
        print("  final verdict:",
              v.status if v is not None else "pending human decision")
        print("  state:   ", t.state.value)
        print("  human cases:", len(t.pending_cases()))
        print("  active invoices (evidence):", billing.find({"status": "issued"}))


if __name__ == "__main__":
    escenario_4(); escenario_5(); escenario_6()
