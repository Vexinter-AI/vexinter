"""Invoice Ninja v5 adapters (§6.3): customers as the 'crm' system and
invoices as the 'billing' system.

Contract established with examples/probe_invoiceninja.py against a real
instance:
- Invoice creation IGNORES 'idempotency_key' (despite the docs):
  'create_invoice' declares supports_idempotency=False.
- Ids are unique only per entity type (a customer and an invoice
  may share an id).
- Verified status_id: 1 draft, 2 sent (= 'issued'), 5 cancelled
  (= 'voided'). Any other value is reported as 'status_<n>'.
- An archived customer still appears in listings (archived_at > 0).
- 'cancel' on a DRAFT responds 422 ("This invoice cannot be cancelled"):
  a draft is ARCHIVED (not deleted); a sent invoice is cancelled.
- Network outage or 5xx: UNKNOWN outcome (IndeterminateOutcome), never
  a definitive failure. A 4xx is a definitive rejection (ok=False).
"""
from __future__ import annotations

import os

import httpx

from .core.events import now
from .core.tools import (
    IndeterminateOutcome,
    Observation,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolCompensation,
    ToolProbe,
    ToolSpec,
)

_STATUS = {"1": "draft", "2": "issued", "5": "voided"}
_PER_PAGE = 100
_PAGINA_POR_DEFECTO = 20


class InvoiceNinjaClient:
    def __init__(self, url: str, token: str, transport=None):
        self._http = httpx.Client(
            base_url=f"{url.rstrip('/')}/api/v1", timeout=30.0,
            transport=transport,
            headers={"X-API-TOKEN": token,
                     "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json",
                     "Content-Type": "application/json"})

    def request(self, method, path, body=None, params=None):
        """Returns (status, body). Network outage and 5xx → IndeterminateOutcome."""
        try:
            r = self._http.request(method, path, json=body, params=params)
        except httpx.TransportError as e:
            raise IndeterminateOutcome(
                f"no response from the instance: {e}") from e
        if r.status_code >= 500:
            raise IndeterminateOutcome(
                f"the instance responded {r.status_code}")
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    def list_all(self, path, params=None):
        """Returns (records, complete). 'complete' is False if the list
        may have been truncated by pagination: then nothing can be
        asserted."""
        status, body = self.request(
            "GET", path, params={**(params or {}), "per_page": _PER_PAGE})
        rows = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(rows, list):
            raise IndeterminateOutcome(f"invalid listing ({status})")
        total = ((body.get("meta") or {}).get("pagination") or {}).get("total")
        if isinstance(total, int):
            complete = total <= len(rows)
        else:
            complete = len(rows) < _PAGINA_POR_DEFECTO
        return rows, complete


def _customer(d: dict) -> dict:
    email = next((c.get("email") for c in (d.get("contacts") or [])
                  if c.get("email")), None)
    if d.get("is_deleted"):
        status = "deleted"
    elif d.get("archived_at"):
        status = "archived"
    else:
        status = "active"
    return {"id": d["id"], "name": d.get("name"), "email": email,
            "status": status}


def _invoice(d: dict) -> dict:
    sid = str(d.get("status_id"))
    if d.get("is_deleted"):
        status = "deleted"
    elif d.get("archived_at"):
        status = "archived"
    else:
        status = _STATUS.get(sid, f"status_{sid}")
    return {"id": d["id"], "number": d.get("number"),
            "customer_id": d.get("client_id"), "amount": d.get("amount"),
            "status": status}


def _matches(record: dict, where: dict) -> bool:
    return all(record.get(k) == v for k, v in where.items())


def _data(body):
    return body.get("data") if isinstance(body, dict) else None


def _fallo(status, body) -> OperationResult:
    return OperationResult(
        ok=False, error=f"the instance responded {status}: {str(body)[:200]}")


def _primero(status, body):
    """Result of a bulk action: list with the affected record."""
    rows = _data(body)
    if status == 200 and isinstance(rows, list) and rows:
        return rows[0]
    return None


def _observar(rows, complete, query, mapper, source) -> Observation:
    q = dict(query or {})
    many = q.pop("many", False)
    where = q.get("where", {})
    recs = [r for r in (mapper(x) for x in rows) if _matches(r, where)]
    data = recs if many else (recs[0] if recs else None)
    return Observation(data=data, observed_at=now(), source=source,
                       consistency="strong" if complete else "unknown")


class InvoiceNinjaCrm:
    system = "crm"

    def __init__(self, client: InvoiceNinjaClient):
        self._c = client

    def tools(self):
        return {
            "create_customer": ToolSpec(
                system="crm", name="create_customer",
                description="Creates (or returns) a customer identified by email.",
                input_schema={"type": "object",
                              "properties": {"name": {"type": "string"},
                                             "email": {"type": "string"}},
                              "required": ["name", "email"]},
                operation_class=OperationClass.IDEMPOTENT_WRITE,
                supports_idempotency=True),
            "archive_customer": ToolSpec(
                system="crm", name="archive_customer",
                description="Archives a customer (compensation).",
                input_schema={"type": "object",
                              "properties": {"customer_id": {"type": "string"}},
                              "required": ["customer_id"]},
                operation_class=OperationClass.REVERSIBLE_WRITE,
                supports_idempotency=True),
        }

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        a = req.args
        if tool == "create_customer":
            # business idempotency: the instance offers no key
            rows, complete = self._c.list_all("/clients", {"email": a["email"]})
            if not complete:
                raise IndeterminateOutcome(
                    "could not rule out an existing customer with that email")
            found = [_customer(r) for r in rows
                     if _customer(r)["email"] == a["email"]]
            if found:
                rec = found[0]
                if rec["status"] != "active":
                    return OperationResult(
                        ok=False, data=rec,
                        error=f"a {rec['status']} customer "
                              "with that email already exists")
                return OperationResult(ok=True, data=rec)
            st, body = self._c.request(
                "POST", "/clients",
                {"name": a["name"], "contacts": [{"email": a["email"]}]})
            d = _data(body)
            if st == 200 and isinstance(d, dict):
                return OperationResult(ok=True, data=_customer(d))
            return _fallo(st, body)
        if tool == "archive_customer":
            st, body = self._c.request("GET", f"/clients/{a['customer_id']}")
            if st == 404:
                return OperationResult(ok=False, error="customer does not exist")
            cur = _data(body)
            if st != 200 or not isinstance(cur, dict):
                return _fallo(st, body)
            if _customer(cur)["status"] == "archived":
                return OperationResult(ok=True, data=_customer(cur))
            st, body = self._c.request(
                "POST", "/clients/bulk",
                {"action": "archive", "ids": [a["customer_id"]]})
            rec = _primero(st, body)
            if rec is None:
                return _fallo(st, body)
            return OperationResult(ok=True, data=_customer(rec))
        return OperationResult(ok=False, error=f"unknown tool: {tool}")

    def observe(self, query: dict) -> Observation:
        where = (query or {}).get("where", {})
        params = ({"email": where["email"]}
                  if isinstance(where.get("email"), str) else {})
        rows, complete = self._c.list_all("/clients", params)
        return _observar(rows, complete, query, _customer, "invoiceninja/api")


class InvoiceNinjaBilling:
    system = "billing"

    def __init__(self, client: InvoiceNinjaClient, approval: str = "auto",
                 once_per_execution: bool = False):
        if approval not in ("auto", "human"):
            raise ValueError("approval must be 'auto' or 'human'")
        self._c = client
        self._approval = approval
        self._once = once_per_execution

    def tools(self):
        return {
            "create_invoice": ToolSpec(
                system="billing", name="create_invoice",
                description="Issues an invoice for a customer.",
                input_schema={"type": "object",
                              "properties": {"customer_id": {"type": "string"},
                                             "amount": {"type": "number"}},
                              "required": ["customer_id", "amount"]},
                operation_class=OperationClass.REVERSIBLE_WRITE,
                supports_idempotency=False,
                retry_approval=self._approval,
                once_per_execution=self._once,
                compensation=ToolCompensation(
                    tool="billing.void_invoice",
                    args_from_result={"invoice_id": "id"}),
                effect_probe=ToolProbe(
                    system="billing",
                    query={"type": "invoice",
                           "where": {"customer_id":
                                     {"$ref": "args.customer_id"}}})),
            "void_invoice": ToolSpec(
                system="billing", name="void_invoice",
                description="Voids an invoice leaving a trail (§7.3): cancels "
                            "a sent one and archives a draft.",
                input_schema={"type": "object",
                              "properties": {"invoice_id": {"type": "string"}},
                              "required": ["invoice_id"]},
                operation_class=OperationClass.REVERSIBLE_WRITE,
                supports_idempotency=True),
        }

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        a = req.args
        if tool == "create_invoice":
            st, body = self._c.request(
                "POST", "/invoices",
                {"client_id": a["customer_id"],
                 "line_items": [{"notes": "Vexinter", "cost": a["amount"],
                                 "quantity": 1}]})
            d = _data(body)
            if st != 200 or not isinstance(d, dict):
                return _fallo(st, body)
            borrador = _invoice(d)
            st2, body2 = self._c.request(
                "POST", "/invoices/bulk",
                {"action": "mark_sent", "ids": [borrador["id"]]})
            rec = _primero(st2, body2)
            if rec is None:
                return OperationResult(
                    ok=False, data=borrador,
                    error=f"the invoice was created as a draft but could not "
                          f"be issued ({st2})")
            return OperationResult(ok=True, data=_invoice(rec))
        if tool == "void_invoice":
            st, body = self._c.request("GET", f"/invoices/{a['invoice_id']}")
            if st == 404:
                return OperationResult(ok=False, error="invoice does not exist")
            cur = _data(body)
            if st != 200 or not isinstance(cur, dict):
                return _fallo(st, body)
            sid = str(cur.get("status_id"))
            ya_anulada = sid == "5" or (
                sid == "1" and (cur.get("archived_at") or cur.get("is_deleted")))
            if ya_anulada:
                return OperationResult(ok=True, data=_invoice(cur))
            accion = "archive" if sid == "1" else "cancel"
            st, body = self._c.request(
                "POST", "/invoices/bulk",
                {"action": accion, "ids": [a["invoice_id"]]})
            rec = _primero(st, body)
            if rec is None:
                return _fallo(st, body)
            return OperationResult(ok=True, data=_invoice(rec))
        return OperationResult(ok=False, error=f"unknown tool: {tool}")

    def observe(self, query: dict) -> Observation:
        where = (query or {}).get("where", {})
        params = ({"client_id": where["customer_id"]}
                  if isinstance(where.get("customer_id"), str) else {})
        rows, complete = self._c.list_all("/invoices", params)
        return _observar(rows, complete, query, _invoice, "invoiceninja/api")


def adapters_from_env():
    """Factory for VEXINTER_ADAPTERS=vexinter.invoiceninja:adapters_from_env

    INVOICENINJA_APPROVAL=human requires human approval to repeat
    invoice creation after an attempt that did not end well.
    INVOICENINJA_ONCE_PER_EXECUTION=1 prevents creating the same invoice
    twice (same arguments) in an execution: the repeat returns what was
    already recorded."""
    approval = os.environ.get("INVOICENINJA_APPROVAL", "auto")
    if approval not in ("auto", "human"):
        raise ValueError("INVOICENINJA_APPROVAL must be 'auto' or 'human'")
    once = os.environ.get("INVOICENINJA_ONCE_PER_EXECUTION") == "1"
    c = InvoiceNinjaClient(os.environ["INVOICENINJA_URL"],
                           os.environ["INVOICENINJA_API_TOKEN"])
    return [InvoiceNinjaCrm(c),
            InvoiceNinjaBilling(c, approval=approval, once_per_execution=once)]