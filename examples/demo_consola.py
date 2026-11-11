"""Populates the service with varied states to walk through the console (§13).

Prerequisite: demo_distribuido has already run (it consumes the initial
false success of billing). Generates three tasks: fulfilled, failed with
compensation, and pending human decision."""
import os
import sys

from examples.demo_coordinado import condicion_coordinada
from vexinter import Vexinter
from vexinter.core.coordinator import CompensatingSpec, Plan, Step
from vexinter.identity import AgentKeyMaterial
from vexinter.remote import RemoteError

ENDPOINT = os.environ.get("VEXINTER_ENDPOINT", "http://localhost:8000")
ADMIN = os.environ.get("VEXINTER_ADMIN_TOKEN", "demo-admin-token")

PERMISOS_ORQUESTADOR = {
    "crm.create_customer", "billing.create_invoice",
    "notify.send_welcome_email",
    "crm.archive_customer", "billing.void_invoice"}


def _enroll(adm):
    try:
        adm.remote.enroll_agent("orquestador",
                                allowed_tools=PERMISOS_ORQUESTADOR)
    except RemoteError as e:
        print(f"could not enroll 'orquestador': {e}")
        print("if it already exists with incomplete permissions, run:")
        print("  docker compose down -v && docker compose up --build -d")
        print("and repeat demo_distribuido before this demo.")
        sys.exit(1)


def _a1(email, nombre, compensable=True):
    return Step(
        id="a1", tool="crm.create_customer",
        args={"name": nombre, "email": email},
        postcondition={"op": "exists", "system": "crm",
                       "query": {"type": "customer",
                                 "where": {"email": email}},
                       "as": "cliente"},
        compensation=(CompensatingSpec(tool="crm.archive_customer",
                                       args_from_result={"customer_id": "id"})
                     if compensable else None))


def _a2():
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


def _a3(depends_on=("a2",)):
    return Step(id="a3", tool="notify.send_welcome_email",
                args_from={"customer_id": "a1.data.id",
                           "email": "a1.data.email"},
                depends_on=list(depends_on))


def _post_recuperacion():
    return {"op": "count", "system": "billing",
            "query": {"type": "invoice",
                      "where": {"customer_id": {"$ref": "steps.a1.data.id"},
                                "status": "issued"}},
            "equals": 0}


def main():
    adm = Vexinter(endpoint=ENDPOINT, admin_token=ADMIN)
    _enroll(adm)
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("orquestador")
    cred = adm.remote.issue_credential(inst["instance_id"], keys)
    vex = Vexinter(endpoint=ENDPOINT, credentials=cred)

    def tarea(objetivo, nombre, email, plan):
        with vex.task(agent="orquestador", objective=objetivo,
                      condition=condicion_coordinada(),
                      input={"email": email}) as t:
            v = t.run_plan(plan)
            print(f"{objetivo}: {v.status if v else 'no verdict'} "
                  f"→ state {t.state.value} ({t.task_id})")

    # 1) happy path → fulfilled
    tarea("Signup and invoice (happy)", "Luis", "luis@ejemplo.com",
          Plan(steps=[_a1("luis@ejemplo.com", "Luis"), _a2()],
               post_recovery_condition=_post_recuperacion()))

    # 2) notification failure → backward compensation → failed
    tarea("Signup, invoice and welcome (compensation)", "Eva", "eva@ejemplo.com",
          Plan(steps=[_a1("eva@ejemplo.com", "Eva"), _a2(), _a3()],
               post_recovery_condition=_post_recuperacion()))

    tarea("Signup and welcome (human decision)", "Sofia", "sofia@example.com",
          Plan(steps=[_a1("sofia@example.com", "Sofia", compensable=False),
                      _a3(depends_on=("a1",))]))

    print("\nConsole: http://localhost:8000/console/login "
          "(token: the value of VEXINTER_ADMIN_TOKEN)")


if __name__ == "__main__":
    main()
