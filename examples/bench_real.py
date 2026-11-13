"""Failure benchmark against a REAL Invoice Ninja (local laboratory).

Reuses the agent and arms from bench_llm, but:
  - CRM and billing are the real adapters (vexinter/invoiceninja.py);
  - a wrapper injects each class's failure on the FIRST invoice creation
    (the effect, when there is one, actually happens on the instance);
  - the oracle reads the final state through the provider's API, bypassing
    the adapter.

Requirements: Invoice Ninja laboratory running and, in this session,
  $env:INVOICENINJA_URL, $env:INVOICENINJA_API_TOKEN and $env:OPENAI_API_KEY.

Usage:
  python -m examples.bench_real --n 1 --clases control,falso_exito --traza
  python -m examples.bench_real --n 10
"""
import argparse
import os
import sys
import uuid

import httpx

import examples.demo_llm as dl
from examples.bench_llm import (
    _TRAZA,
    BRAZOS,
    COLS,
    _Agente,
    _cerrar,
    _ConLectura,
    _Directa,
    _resumen,
    imprimir_tabla,
)
from examples.demo_coordinado import condicion_coordinada
from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.core.tools import IndeterminateOutcome, Observation, OperationResult
from vexinter.invoiceninja import (
    InvoiceNinjaBilling,
    InvoiceNinjaClient,
    InvoiceNinjaCrm,
)

URL = os.environ.get("INVOICENINJA_URL", "").rstrip("/")
TOKEN = os.environ.get("INVOICENINJA_API_TOKEN", "")
if not URL or not TOKEN:
    sys.exit("INVOICENINJA_URL and INVOICENINJA_API_TOKEN are missing from the environment.")
if not (URL.startswith(("http://localhost", "http://127.0.0.1"))):
    sys.exit("This benchmark only runs against a local laboratory instance.")

CLASES = ("control", "falso_exito", "efecto_filtrado", "respuesta_perdida",
          "corte_antes", "ciego_honesto", "ciego_falso_exito")
TAG = uuid.uuid4().hex[:8]      # so two script runs do not collide


class FallaInyectada:
    """Wraps the REAL invoice adapter and injects the class's failure on the
    first invoice creation (subsequent ones are real)."""
    system = "billing"

    def __init__(self, real, clase, confusa=False):
        self._real, self.clase, self._hecho = real, clase, False
        self._confusa = confusa

    def tools(self):
        t = dict(self._real.tools())
        if self._confusa:
            t["create_invoice"] = t["create_invoice"].model_copy(update={
                "description": "Creates and issues an invoice (two calls: "
                               "create and mark as sent). The instance "
                               "offers no idempotency."})
        return t

    def observe(self, query):
        if self.clase.startswith("ciego"):
            return Observation(data=None, observed_at=now(),
                               source="billing/none", consistency="unknown")
        return self._real.observe(query)

    @staticmethod
    def _falsa(args):
        return {"id": uuid.uuid4().hex[:10], "number": "9999",
                "customer_id": args.get("customer_id"),
                "amount": args.get("amount"), "status": "issued"}

    def invoke(self, tool, req):
        if tool != "create_invoice" or self.clase in ("control",
                                                      "ciego_honesto"):
            return self._real.invoke(tool, req)
        primera = not self._hecho
        self._hecho = True
        if self.clase == "ciego_falso_exito" or (primera
                                                 and self.clase == "falso_exito"):
            return OperationResult(ok=True, data=self._falsa(req.args))
        if not primera:
            return self._real.invoke(tool, req)
        if self.clase == "corte_antes":
            raise IndeterminateOutcome("cut before the effect (injected)")
        res = self._real.invoke(tool, req)          # the effect really happens
        if self.clase == "respuesta_perdida":
            raise IndeterminateOutcome("lost response (injected)")
        if self.clase == "efecto_filtrado":
            return OperationResult(ok=False, error="simulated timeout",
                                   data=res.data)
        return res


def _directo():
    """Own HTTP client, bypassing the adapter: the provider's route."""
    return httpx.Client(
        base_url=f"{URL}/api/v1", timeout=30.0,
        headers={"X-API-TOKEN": TOKEN, "X-Requested-With": "XMLHttpRequest",
                 "Accept": "application/json",
                 "Content-Type": "application/json"})


def oraculo_real(email):
    """Truth read through the provider's API: active customer and EXACTLY one
    sent invoice (status_id 2), not archived or deleted."""
    with _directo() as d:
        r = d.get("/clients", params={"email": email, "per_page": 100})
        r.raise_for_status()
        clientes = [c for c in r.json().get("data", [])
                    if any(k.get("email") == email
                           for k in (c.get("contacts") or []))
                    and not c.get("is_deleted") and not c.get("archived_at")]
        if not clientes:
            return False, 0
        r = d.get("/invoices", params={"client_id": clientes[0]["id"],
                                       "per_page": 100})
        r.raise_for_status()
        emitidas = [f for f in r.json().get("data", [])
                    if str(f.get("status_id")) == "2"
                    and not f.get("is_deleted") and not f.get("archived_at")]
    return len(emitidas) == 1, len(emitidas)


def ejecutar_real(llm, clase, brazo, i, verificar=False, aprobacion="auto",
                  una_vez=False, confusa=False):
    con_vexinter, con_lectura = BRAZOS[brazo]
    email = f"real-{TAG}-{clase}-{brazo}-{i}@example.com".replace("+", "_")
    cliente = InvoiceNinjaClient(URL, TOKEN)
    crm = InvoiceNinjaCrm(cliente)
    inner = FallaInyectada(
        InvoiceNinjaBilling(cliente, aprobacion=aprobacion, una_vez=una_vez),
        clase, confusa)
    billing = _ConLectura(inner) if con_lectura else inner
    allowed = set(dl.HERRAMIENTAS_AGENTE)
    if con_lectura:
        allowed.add("billing.list_invoices")
    objetivo = dl.OBJETIVO_ALTA
    if verificar and con_lectura:
        objetivo += (" Before considering the task finished, use the invoice "
                     "read tool to check that the invoice actually exists.")
    contexto = {"email": email, "nombre": "Ana"}
    _TRAZA.clear()
    escalado = False
    if con_vexinter:
        vex = Vexinter(embedded=True, adapters=[crm, billing])
        with vex.task(agent="agente_llm", objective=dl.OBJETIVO_ALTA,
                      condition=condicion_coordinada(),
                      input={"email": email}) as t:
            rep = llm.run(t, objetivo, contexto, allowed=allowed)
            agente = rep.done is True
            v = _cerrar(t, inner)
            afirma = v.status == "met"
            escalado = len(t.pending_cases()) > 0
    else:
        rep = llm.run(_Directa([crm, billing]), objetivo, contexto,
                      allowed=allowed)
        agente = afirma = rep.done is True
    logrado, emitidas = oraculo_real(email)
    fila = {"silenciosas": int(afirma and not logrado),
            "subestima": int((not afirma) and logrado),
            "duplicadas": int(emitidas > 1),
            "escaladas": int(escalado),
            "logradas": int(logrado),
            "errores": 0}
    linea = (f"agent={int(agente)} claims={int(afirma)} "
             f"achieved={int(logrado)} issued={emitidas} "
             f"escalated={int(escalado)}")
    traza = " | ".join(
        f"{t}({_resumen(a)})->{'ok' if ok else 'ERR'}"
        for t, a, ok in _TRAZA)
    return fila, linea, traza


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10,
                    help="repetitions per class and arm")
    ap.add_argument("--clases", default=",".join(CLASES))
    ap.add_argument("--model")
    ap.add_argument("--temperatura", type=float, default=1.0)
    ap.add_argument("--verificar", action="store_true")
    ap.add_argument("--traza", action="store_true")
    ap.add_argument("--aprobacion", choices=("auto", "human"), default="auto")
    ap.add_argument("--una-vez", action="store_true",
                    help="billing declares 'once per execution'")
    ap.add_argument("--descripcion-confusa", action="store_true",
                    help="uses a create_invoice description that nudges the "
                         "agent into repeating the call")
    ap.add_argument("--max-entrada", type=int, default=1_500_000)
    ap.add_argument("--max-salida", type=int, default=150_000)
    a = ap.parse_args()

    clases = [c.strip() for c in a.clases.split(",") if c.strip()]
    for c in clases:
        if c not in CLASES:
            sys.exit(f"unknown class: {c}. Valid: {', '.join(CLASES)}")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit('Missing OPENAI_API_KEY: $env:OPENAI_API_KEY = "sk-..."')

    llm = _Agente(model=a.model, temperatura=a.temperatura)
    print(f"model={llm._model} temperature={a.temperatura} "
          f"approval={a.aprobacion} once={a.una_vez} "
          f"confusing_description={a.descripcion_confusa} "
          f"instance={URL} tag={TAG}\n")
    tabla = {(c, b): dict.fromkeys(COLS + ("n",), 0)
             for c in clases for b in BRAZOS}
    total = a.n * len(clases) * len(BRAZOS)
    k, corte = 0, False
    for i in range(a.n):
        for clase in clases:
            for brazo in BRAZOS:
                if (llm.usage["prompt"] >= a.max_entrada
                        or llm.usage["completion"] >= a.max_salida):
                    corte = True
                    break
                k += 1
                try:
                    fila, linea, traza = ejecutar_real(
                        llm, clase, brazo, i, a.verificar, a.aprobacion,
                        a.una_vez, a.descripcion_confusa)
                except Exception as e:
                    fila = dict.fromkeys(COLS, 0)
                    fila["errores"] = 1
                    linea = f"ERROR {type(e).__name__}: {str(e)[:160]}"
                    traza = ""
                for c in COLS:
                    tabla[(clase, brazo)][c] += fila[c]
                tabla[(clase, brazo)]["n"] += 1
                print(f"[{k}/{total}] {clase:<18}{brazo:<14}{linea}"
                      f"  | tokens {llm.usage['prompt']}"
                      f"+{llm.usage['completion']}", flush=True)
                if a.traza and traza:
                    print(f"      {traza}", flush=True)
            if corte:
                break
        if corte:
            break

    print()
    if corte:
        print("TOKEN CAP REACHED: the run stopped before "
              "finishing.\n")
    imprimir_tabla(tabla, clases)
    print(f"\ntokens: input={llm.usage['prompt']}, "
          f"output={llm.usage['completion']}")


if __name__ == "__main__":
    main()
