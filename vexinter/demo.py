from __future__ import annotations

from .core.events import new_id, now
from .core.tools import (
    Observation,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolProbe,
    ToolSpec,
)


def _matches(record: dict, where: dict) -> bool:
    return all(record.get(k) == v for k, v in where.items())


class CrmSystem:
    """Test CRM: idempotent creation and archiving.

    §7.3: the compensation for 'create_customer' is 'archive_customer' —
    it marks the state, it does NOT delete the business record.
    """
    system = "crm"

    def __init__(self):
        self._customers: dict[str, dict] = {}
        self._idem: dict[str, dict] = {}

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
        if tool == "create_customer":
            key = req.idempotency_key
            if key and key in self._idem:                      # dedup (§5.2)
                return OperationResult(ok=True, data=self._idem[key])
            for c in self._customers.values():                # business idempotency
                if c["email"] == req.args["email"]:
                    if key: self._idem[key] = c
                    return OperationResult(ok=True, data=c)
            cust = {"id": new_id("cus"), "name": req.args["name"],
                    "email": req.args["email"], "status": "active"}
            self._customers[cust["id"]] = cust
            if key: self._idem[key] = cust
            return OperationResult(ok=True, data=cust)
        if tool == "archive_customer":
            existing = self._customers.get(req.args["customer_id"])
            if existing is None:
                return OperationResult(ok=False, error="nonexistent customer")
            existing["status"] = "archived"
            return OperationResult(ok=True, data=existing)
        return OperationResult(ok=False, error=f"unknown tool: {tool}")

    def observe(self, query: dict) -> Observation:
        q = dict(query); many = q.pop("many", False)
        where = q.get("where", {})
        found = [c for c in self._customers.values() if _matches(c, where)]
        data = found if many else (found[0] if found else None)
        return Observation(data=data, observed_at=now(),
                           source="crm/primary", consistency="strong")


class BillingSystem:
    """Test billing.

    fail_first       : N invocations return success WITHOUT persisting (§1).
    leak_on_fail     : the invocation returns an error BUT persists:
                       ambiguous effect (§7.2) that no recovery can
                       ignore without breaking the invariants.
    read_consistency : "eventual" simulates a lagging read replica (§7.2).
    """
    system = "billing"

    def __init__(self, fail_first=0, supports_idempotency=True,
                 read_consistency="strong", leak_on_fail=False,
                 sondeable=False, aprobacion="auto", una_vez=False):
        self._invoices: dict[str, dict] = {}
        self._idem: dict[str, dict] = {}
        self._fail_first = fail_first
        self._leak = leak_on_fail
        self._read_consistency = read_consistency
        self._synced = True
        self._supports_idem = supports_idempotency
        self._sondeable = sondeable   
        self._aprobacion = aprobacion   
        self._sondeo_ok = True 
        self._una_vez = una_vez

    def tools(self):
        return {
            "create_invoice": ToolSpec(
                system="billing", name="create_invoice",
                description="Issues an invoice for a customer.",
                input_schema={"type": "object",
                              "properties": {"customer_id": {"type": "string"},
                                             "amount": {"type": "number"}},
                              "required": ["customer_id", "amount"]},
                operation_class=OperationClass.IDEMPOTENT_WRITE,
                supports_idempotency=self._supports_idem,
                retry_approval=self._aprobacion,
                once_per_execution=self._una_vez,
                effect_probe=(ToolProbe(
                    system="billing",
                    query={"type": "invoice",
                           "where": {"customer_id": (
                               {"$ref": "args.customer_id"}
                               if self._sondeo_ok else "__ninguno__")}})
                    if self._sondeable else None)),
            "void_invoice": ToolSpec(
                system="billing", name="void_invoice",
                description="Voids an invoice leaving an accounting trail (§7.3).",
                input_schema={"type": "object",
                              "properties": {"invoice_id": {"type": "string"}},
                              "required": ["invoice_id"]},
                operation_class=OperationClass.REVERSIBLE_WRITE,
                supports_idempotency=True),
        }

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        if tool == "create_invoice":
            if self._fail_first > 0:
                self._fail_first -= 1
                if self._leak:
                    # persists but reports error → ambiguous effect (§7.2)
                    inv = {"id": new_id("inv"), "status": "issued",
                           "customer_id": req.args["customer_id"],
                           "amount": req.args["amount"]}
                    self._invoices[inv["id"]] = inv
                    return OperationResult(ok=False, error="simulated timeout",
                                           data=inv)
                # false success: responds ok without persisting (§1)
                return OperationResult(ok=True,
                                       data={"id": new_id("inv"),
                                             "status": "created",
                                             "_simulado": True})
            key = req.idempotency_key
            if key and key in self._idem:
                return OperationResult(ok=True, data=self._idem[key])
            inv = {"id": new_id("inv"), "customer_id": req.args["customer_id"],
                   "amount": req.args["amount"], "status": "issued"}
            self._invoices[inv["id"]] = inv
            if key: self._idem[key] = inv
            self._synced = (self._read_consistency == "strong")
            return OperationResult(ok=True, data=inv)
        if tool == "void_invoice":
            existing_inv = self._invoices.get(req.args["invoice_id"])
            if existing_inv is None:
                return OperationResult(ok=False, error="nonexistent invoice")
            # §7.3: it is voided but the original issue is NOT deleted
            existing_inv["status"] = "voided"
            return OperationResult(ok=True, data=existing_inv)
        return OperationResult(ok=False, error=f"unknown tool: {tool}")

    def observe(self, query: dict) -> Observation:
        q = dict(query); many = q.pop("many", False)
        where = q.get("where", {})
        up_to_date = (self._read_consistency == "strong") or self._synced
        view = self._invoices if up_to_date else {}
        found = [i for i in view.values() if _matches(i, where)]
        data = found if many else (found[0] if found else None)
        return Observation(data=data, observed_at=now(),
                           source="billing/replica-1",
                           consistency="strong" if up_to_date else "eventual")

    # -- utilities for tests and demo -------------------------------------
    def tick(self):
        """The replica catches up with the primary (advances simulated time)."""
        self._synced = True

    def simulate_external_sync(self, args: dict):
        inv = {"id": new_id("inv"), "customer_id": args["customer_id"],
               "amount": args.get("amount", 0), "status": "issued",
               "origen": "sincronizador"}
        self._invoices[inv["id"]] = inv
        self._synced = (self._read_consistency == "strong")
        return inv

    def find(self, where: dict) -> list[dict]:
        return [i for i in self._invoices.values() if _matches(i, where)]


class NotifierSystem:
    """Test notifications: persistent failure with external effect (§5.1)."""
    system = "notify"

    def __init__(self, fail: bool = True):
        self._fail = fail
        self._sent: list[dict] = []

    def tools(self):
        return {"send_welcome_email": ToolSpec(
            system="notify", name="send_welcome_email",
            description="Sends a welcome email.",
            input_schema={"type": "object",
                          "properties": {"customer_id": {"type": "string"},
                                         "email": {"type": "string"}},
                          "required": ["customer_id", "email"]},
            operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE,
            supports_idempotency=False)}

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        if self._fail:
            return OperationResult(ok=False, error="SMTP down (simulated)")
        self._sent.append(req.args)
        return OperationResult(ok=True, data={"sent": True})

    def observe(self, query: dict) -> Observation:
        return Observation(data=None, observed_at=now(),
                           source="notify/none", consistency="unknown")
    
CrmAdapter = CrmSystem
BillingAdapter = BillingSystem