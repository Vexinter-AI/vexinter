import os
import uuid

import httpx
import pytest

from examples.demo_coordinated import coordinated_condition
from vexinter import Vexinter
from vexinter.core.statemachine import ExecutionState
from vexinter.invoiceninja import (
    InvoiceNinjaBilling,
    InvoiceNinjaClient,
    InvoiceNinjaCrm,
)

URL = os.environ.get("INVOICENINJA_URL", "").rstrip("/")
TOKEN = os.environ.get("INVOICENINJA_API_TOKEN", "")
pytestmark = pytest.mark.skipif(
    not (URL.startswith(("http://localhost", "http://127.0.0.1"))) or not TOKEN,
    reason="requires a local Invoice Ninja instance "
           "(INVOICENINJA_URL / INVOICENINJA_API_TOKEN)")


def _vex():
    c = InvoiceNinjaClient(URL, TOKEN)
    return Vexinter(embedded=True,
                    adapters=[InvoiceNinjaCrm(c), InvoiceNinjaBilling(c)])


def _directo():
    """Own HTTP client, bypassing the adapter: the provider-side route."""
    return httpx.Client(
        base_url=f"{URL}/api/v1", timeout=30.0,
        headers={"X-API-TOKEN": TOKEN, "X-Requested-With": "XMLHttpRequest",
                 "Accept": "application/json",
                 "Content-Type": "application/json"})


def _email():
    return f"vex-{uuid.uuid4().hex[:10]}@example.com"


def test_flujo_real_cumple():
    email = _email()
    with _vex().task(agent="a", objective="o", condition=coordinated_condition(),
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        assert t.audit().ok


def test_cancel_by_outside_of_adapter_se_detects():
    """ The verifier reads on its own; it does not trust what reported
    la tool. Si un change external void la invoice, lo ve."""
    email = _email()
    with _vex().task(agent="a", objective="o", condition=coordinated_condition(),
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        f = t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        with _directo() as d:
            r = d.post("/invoices/bulk",
                       json={"action": "cancel", "ids": [f.id]})
            assert r.status_code == 200
        assert t.verify().status == "not_met"
        assert t.state is ExecutionState.FAILED


def test_replay_protects_a_a_system_without_idempotency():
    """The instance ignores idempotency_key; the replay layer
    prevents a lost response from creating a second invoice."""
    email = _email()
    with _vex().task(agent="a", objective="o", condition=coordinated_condition(),
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        inter, ctx = t._rt.intermediary, t._run.call_context()
        args = {"customer_id": c.id, "amount": 100}
        r1 = inter.call(ctx, "billing.create_invoice", args, client_key="k1")
        r2 = inter.call(ctx, "billing.create_invoice", args, client_key="k1")
        assert r1.ok and r2.ok
        assert r2.provenance.invoked_event == r1.provenance.invoked_event
        with _directo() as d:
            filas = d.get("/invoices", params={"client_id": c.id}).json()["data"]
        assert len(filas) == 1


def test_void_a_draft_lo_archives_y_leaves_trace():
    """The instance refuses to cancel a draft (422): the compensation for
    an invoice created but not issued must work the same."""
    email = _email()
    cond = {"op": "all", "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "customer"},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "customer.id"},
                             "status": "issued"}},
         "equals": 0}]}
    with _vex().task(agent="a", objective="o", condition=cond,
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        with _directo() as d:
            r = d.post("/invoices", json={
                "client_id": c.id,
                "line_items": [{"notes": "draft", "cost": 100,
                                "quantity": 1}]})
            assert r.status_code == 200
            iid = r.json()["data"]["id"]
            assert r.json()["data"]["status_id"] == "1"
        res = t.tools.billing.void_invoice({"invoice_id": iid})
        assert res.ok
        with _directo() as d:
            fila = d.get(f"/invoices/{iid}").json()["data"]
        assert fila["status_id"] == "1" and fila["archived_at"]
        assert t.verify().status == "met"


def test_conformance_of_probe_of_invoices_against_the_real_instance():
    """The adapter's declared probe sees the effect of a real write."""
    email = _email()
    with _vex().task(agent="a", objective="o", condition=coordinated_condition(),
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        inter = t._rt.intermediary
        spec = inter.spec_of("billing.create_invoice")
        args = {"customer_id": c.id, "amount": 100}
        assert inter._probe(spec, args)["count"] == 0
        assert t.tools.billing.create_invoice(args).ok
        assert inter._probe(spec, args)["count"] == 1