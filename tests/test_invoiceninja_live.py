import os
import uuid

import httpx
import pytest

from examples.demo_coordinado import condicion_coordinada
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
    reason="requiere una instancia local de Invoice Ninja "
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
    with _vex().task(agent="a", objective="o", condition=condicion_coordinada(),
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        assert t.audit().ok


def test_cancelar_por_fuera_del_adaptador_se_detecta():
    """§4.1: the verifier reads on its own; it does not trust what reported
    la herramienta. Si un cambio externo anula la factura, lo ve."""
    email = _email()
    with _vex().task(agent="a", objective="o", condition=condicion_coordinada(),
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


def test_replay_protege_a_un_sistema_sin_idempotencia():
    """§2.1: la instancia ignora idempotency_key; la capa de replay evita
    que una respuesta perdida cree una segunda factura."""
    email = _email()
    with _vex().task(agent="a", objective="o", condition=condicion_coordinada(),
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


def test_anular_un_borrador_lo_archiva_y_deja_rastro():
    """The instance refuses to cancel a draft (422): the compensation for
    una factura creada pero no emitida tiene que funcionar igual."""
    email = _email()
    cond = {"op": "all", "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "cliente"},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "cliente.id"},
                             "status": "issued"}},
         "equals": 0}]}
    with _vex().task(agent="a", objective="o", condition=cond,
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        with _directo() as d:
            r = d.post("/invoices", json={
                "client_id": c.id,
                "line_items": [{"notes": "borrador", "cost": 100,
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


def test_conformidad_del_sondeo_de_facturas_contra_la_instancia_real():
    """El sondeo declarado por el adaptador ve el efecto de una escritura real."""
    email = _email()
    with _vex().task(agent="a", objective="o", condition=condicion_coordinada(),
                     input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Laboratorio", "email": email})
        inter = t._rt.intermediary
        spec = inter.spec_of("billing.create_invoice")
        args = {"customer_id": c.id, "amount": 100}
        assert inter._probe(spec, args)["count"] == 0
        assert t.tools.billing.create_invoice(args).ok
        assert inter._probe(spec, args)["count"] == 1