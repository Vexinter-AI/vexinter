"""Failure benchmark (§1, §4, §5): what happens with each class of failure
depending on the agent's policy. All simulated, zero cost.

The TRUTH (oracle) is read from the simulated systems' internal state, not
from the verifier: so Vexinter's verdict is checked against something
independent of it.

Columns:
  silenciosas : the agent claims success but the objective was NOT achieved
  subestima   : the agent does not claim success although the objective WAS achieved
  duplicadas  : there is more than one issued invoice (repeated effect)
  escaladas   : the case was left waiting for a person
  logradas    : the objective was truly achieved (per the oracle)

Usage:  python -m examples.bench_fallos [--n 1]
"""
import argparse
from datetime import timedelta

from examples.demo_coordinado import condicion_coordinada
from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.core.tools import IndeterminateOutcome, Observation, OperationRequest
from vexinter.demo import BillingSystem, CrmSystem


class _PierdeRespuesta(BillingSystem):
    """The effect happens and the response is lost (only the first time)."""

    def __init__(self):
        super().__init__()
        self._hecho = False

    def invoke(self, tool, req):
        res = super().invoke(tool, req)
        if tool == "create_invoice" and not self._hecho:
            self._hecho = True
            raise IndeterminateOutcome("lost response (simulated)")
        return res


class _CorteAntes(BillingSystem):
    """Cut BEFORE the effect: nothing is persisted (only the first time)."""

    def __init__(self):
        super().__init__()
        self._hecho = False

    def invoke(self, tool, req):
        if tool == "create_invoice" and not self._hecho:
            self._hecho = True
            raise IndeterminateOutcome("cut before the effect (simulated)")
        return super().invoke(tool, req)


class _Ciego(BillingSystem):
    """Cannot be observed (§6.3): its read declares 'unknown', now and
    later. Writes follow BillingSystem's behavior."""

    def observe(self, query):
        return Observation(data=None, observed_at=now(),
                           source="billing/none", consistency="unknown")


FALLAS = {
    "control": lambda: BillingSystem(),
    "falso_exito": lambda: BillingSystem(fail_first=1),
    "efecto_filtrado": lambda: BillingSystem(
        fail_first=1, leak_on_fail=True, supports_idempotency=False,
        sondeable=True),
    "respuesta_perdida": _PierdeRespuesta,
    "corte_antes": _CorteAntes,
    "replica_atrasada": lambda: BillingSystem(read_consistency="eventual"),
    "ciego_honesto": lambda: _Ciego(),
    "ciego_falso_exito": lambda: _Ciego(fail_first=10**6),
}
BRAZOS = ("confiar", "reintentar", "relee", "relee_espera",
          "relee_sin_reintento", "vexinter")
COLS = ("silenciosas", "subestima", "duplicadas", "escaladas", "logradas")


def oraculo(crm, billing, email):
    """Scenario truth: active customer and EXACTLY one issued invoice.
    Reads the systems' primary state, not the adapter's read."""
    cliente = crm.observe({"type": "customer", "where": {"email": email}}).data
    if not cliente or cliente["status"] != "active":
        return False, 0
    emitidas = billing.find({"customer_id": cliente["id"], "status": "issued"})
    return len(emitidas) == 1, len(emitidas)


def _alta_cliente(crm, email):
    r = crm.invoke("create_customer", OperationRequest(
        tool="crm.create_customer", args={"name": "Ana", "email": email}))
    return r.data["id"] if r.ok else None


def _emitir(billing, cid):
    """A direct call, without an idempotency key. Returns ok, or None if
    there was no definitive response."""
    try:
        return billing.invoke("create_invoice", OperationRequest(
            tool="billing.create_invoice",
            args={"customer_id": cid, "amount": 100})).ok
    except IndeterminateOutcome:
        return None


def _visible(billing, cid):
    obs = billing.observe({"type": "invoice", "where": {
        "customer_id": cid, "status": "issued"}})
    return obs.data is not None


def brazo_confiar(crm, billing, email):
    cid = _alta_cliente(crm, email)
    return cid is not None and _emitir(billing, cid) is True, False


def brazo_reintentar(crm, billing, email, max_retries=2):
    cid = _alta_cliente(crm, email)
    if cid is None:
        return False, False
    for _ in range(1 + max_retries):
        if _emitir(billing, cid) is True:
            return True, False
    return False, False


def brazo_relee(crm, billing, email):
    """Naive self-verification: if it doesn't see it, rewrite it."""
    cid = _alta_cliente(crm, email)
    if cid is None:
        return False, False
    _emitir(billing, cid)
    if not _visible(billing, cid):
        _emitir(billing, cid)
    return _visible(billing, cid), False


def brazo_relee_espera(crm, billing, email):
    """If it doesn't see it, WAIT (the simulator lets time pass with tick())
    and re-read before rewriting. NOTE: tick() is a simulator resource; a
    real agent cannot force a replica to catch up."""
    cid = _alta_cliente(crm, email)
    if cid is None:
        return False, False
    _emitir(billing, cid)
    if not _visible(billing, cid):
        if hasattr(billing, "tick"):
            billing.tick()
        if not _visible(billing, cid):
            _emitir(billing, cid)
    return _visible(billing, cid), False


def brazo_relee_sin_reintento(crm, billing, email):
    """Prudent self-verification: does not write again if it cannot confirm;
    reports that it could not."""
    cid = _alta_cliente(crm, email)
    if cid is None:
        return False, False
    _emitir(billing, cid)
    return _visible(billing, cid), False


def brazo_vexinter(crm, billing, email):
    vex = Vexinter(embedded=True, adapters=[crm, billing])
    with vex.task(agent="agente", objective="signup and invoice",
                  condition=condicion_coordinada(),
                  input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Ana", "email": email})
        try:
            t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        except IndeterminateOutcome:
            pass
        v = t.verify()
        if v.status == "not_met":
            t.recover(failed_tool="billing.create_invoice")
            v = t.verify()
        elif v.status == "inconclusive":
            if hasattr(billing, "tick"):
                billing.tick()
            for k in range(1, 4):       # 3 reviews: exhaust the attempts
                r = t.run_followups(at=now() + timedelta(seconds=61 * k))
                if r:
                    v = r[-1]
                    if v.status != "inconclusive":
                        break
        return v.status == "met", len(t.pending_cases()) > 0


_BRAZO_FN = {"confiar": brazo_confiar, "reintentar": brazo_reintentar,
             "relee": brazo_relee, "relee_espera": brazo_relee_espera,
             "relee_sin_reintento": brazo_relee_sin_reintento,
             "vexinter": brazo_vexinter}


def correr(clase, brazo, n):
    tot = dict.fromkeys(COLS, 0)
    for i in range(n):
        crm, billing = CrmSystem(), FALLAS[clase]()
        email = f"bench-{clase}-{brazo}-{i}@example.com"
        afirma, escalado = _BRAZO_FN[brazo](crm, billing, email)
        logrado, emitidas = oraculo(crm, billing, email)
        tot["silenciosas"] += int(afirma and not logrado)
        tot["subestima"] += int((not afirma) and logrado)
        tot["duplicadas"] += int(emitidas > 1)
        tot["escaladas"] += int(escalado)
        tot["logradas"] += int(logrado)
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1,
                    help="repetitions per class and arm")
    n = ap.parse_args().n
    print(f"{'class':<18}{'arm':<21}" + "".join(f"{c:>13}" for c in COLS))
    suma = {b: dict.fromkeys(COLS, 0) for b in BRAZOS}
    for clase in FALLAS:
        for brazo in BRAZOS:
            r = correr(clase, brazo, n)
            for c in COLS:
                suma[brazo][c] += r[c]
            print(f"{clase:<18}{brazo:<21}"
                  + "".join(f"{r[c]:>13}" for c in COLS))
        print()
    print(f"TOTAL ({n} repetition(s) per class and arm)")
    for brazo in BRAZOS:
        print(f"{'':<18}{brazo:<21}"
              + "".join(f"{suma[brazo][c]:>13}" for c in COLS))


if __name__ == "__main__":
    main()
