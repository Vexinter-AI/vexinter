"""Arm with a real model from the failure benchmark (§1, §4, §5).

A real agent (OpenAI, tool calling) executes the task against the same
simulated systems and the same failure classes as examples/bench_fallos.py.
Four arms, with and without Vexinter and with and without a read tool that
lets the agent self-verify:

  solo          agent -> systems, no registry, no keys, no verification
  solo+lee      same, with billing.list_invoices (read-only)
  vexinter      agent -> Vexinter; then verify / recover / reviews
  vexinter+lee  same, with billing.list_invoices

The TRUTH (oracle) comes from the simulators' internal state, not from the
verifier. Each arm is an independent run of the agent.

VALIDITY RULE: in the 'control' class (no failures) all arms must achieve
the objective in every run. If not, the agent is misconfigured and the rest
of the table cannot be interpreted.

Requirements:
  pip install -e ".[llm]"
  $env:OPENAI_API_KEY = "sk-..."        (never in the code)

Usage:
  python -m examples.bench_llm --clases control --n 2 --traza
  python -m examples.bench_llm --n 5
"""
import argparse
import os
import sys
from datetime import timedelta

import examples.demo_llm as dl
from examples.bench_fallos import FALLAS as _FALLAS_BASE
from examples.bench_fallos import oraculo
from examples.demo_coordinado import condicion_coordinada
from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.core.tools import (
    IndeterminateOutcome,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolSpec,
)
from vexinter.demo import BillingSystem, CrmSystem


class _PierdeRespuestaSinIdem(BillingSystem):
    """No idempotency: the effect happens and the response is lost (once)."""

    def __init__(self):
        super().__init__(supports_idempotency=False, sondeable=True)
        self._hecho = False

    def invoke(self, tool, req):
        res = super().invoke(tool, req)
        if tool == "create_invoice" and not self._hecho:
            self._hecho = True
            raise IndeterminateOutcome("lost response (simulated)")
        return res


class _CorteAntesSinIdem(BillingSystem):
    """No idempotency: cut BEFORE the effect (once); nothing is persisted."""

    def __init__(self):
        super().__init__(supports_idempotency=False, sondeable=True)
        self._hecho = False

    def invoke(self, tool, req):
        if tool == "create_invoice" and not self._hecho:
            self._hecho = True
            raise IndeterminateOutcome("cut before the effect (simulated)")
        return super().invoke(tool, req)


class _RespuestaAmbigua(BillingSystem):
    """The write DOES happen and returns ok=True, but the response does not say
    what was created and is left 'pending confirmation' (like an async API): a
    prudent agent may repeat the call to confirm."""

    def invoke(self, tool, req):
        res = super().invoke(tool, req)
        if tool == "create_invoice" and res.ok:
            return OperationResult(ok=True, data={
                "received": True, "status": "pending confirmation"})
        return res


FALLAS = {**_FALLAS_BASE,
          "respuesta_perdida_sin_idem": _PierdeRespuestaSinIdem,
          "corte_antes_sin_idem": _CorteAntesSinIdem,
          "respuesta_ambigua": _RespuestaAmbigua}

BRAZOS = {
    "solo": (False, False),
    "solo+lee": (False, True),
    "vexinter": (True, False),
    "vexinter+lee": (True, True),
}
COLS = ("silenciosas", "subestima", "duplicadas", "escaladas", "logradas",
        "errores")

_TRAZA = []        # (tool, arguments, ok) of the current run


class _R:
    def __init__(self, ok, data, error):
        self.ok, self.data, self.error = ok, data, error


class _Directa:
    """Without Vexinter: the agent calls the systems as-is, with no
    registry, no idempotency keys and no verification."""

    def __init__(self, adapters):
        self._by = {a.system: a for a in adapters}
        self.available_tools = {f"{a.system}.{n}": s
                                for a in adapters
                                for n, s in a.tools().items()}

    def call(self, tool_key, args):
        system, _, name = tool_key.partition(".")
        return self._by[system].invoke(
            name, OperationRequest(tool=tool_key, args=args))


class _ConLectura:
    """Also exposes a READ-ONLY tool so the agent can self-verify. Delegates
    everything else to the real system."""
    system = "billing"

    def __init__(self, inner):
        self._in = inner

    def tools(self):
        t = dict(self._in.tools())
        t["list_invoices"] = ToolSpec(
            system="billing", name="list_invoices",
            description="Lists a customer's invoices (read-only).",
            input_schema={"type": "object",
                          "properties": {"customer_id": {"type": "string"}},
                          "required": ["customer_id"]},
            operation_class=OperationClass.READ)
        return t

    def invoke(self, tool, req):
        if tool == "list_invoices":
            obs = self._in.observe({
                "type": "invoice", "many": True,
                "where": {"customer_id": req.args["customer_id"]}})
            return OperationResult(ok=True, data={
                "invoices": obs.data or [], "lectura": obs.consistency})
        return self._in.invoke(tool, req)

    def observe(self, query):
        return self._in.observe(query)


# -- demo_llm's agent calls _dispatch(); we wrap it so a network cut or an
#    error reaches the model as a tool result (same in all four arms), to
#    strip the simulator's '_simulado' marker (it would be a hint) and to
#    keep a trace.
_dispatch_original = dl._dispatch


def _limpio(data):
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k != "_simulado"}
    return data


def _dispatch(task, tool_key, args):
    try:
        if isinstance(task, _Directa):
            res = task.call(tool_key, args)
        else:
            res = _dispatch_original(task, tool_key, args)
        out = _R(res.ok, _limpio(res.data), res.error)
    except IndeterminateOutcome:
        out = _R(False, None, "there was no definitive response from the "
                              "tool: it is unknown whether the operation "
                              "was performed")
    except Exception as e:
        out = _R(False, None, f"{type(e).__name__}: {e}")
    visto = out.error if out.error else out.data
    base = dict(args) if isinstance(args, dict) else {}
    _TRAZA.append((tool_key, {**base, "ret": str(visto)}, out.ok))
    return out


dl._dispatch = _dispatch


def _resumen(args):
    if not isinstance(args, dict):
        return str(args)[:40]
    partes = []
    for k, v in args.items():
        txt = str(v)[:(110 if k == "ret" else 26)]
        partes.append(f"{k}={txt}".encode("ascii", "replace").decode())
    return ",".join(partes)


class _Agente(dl.OpenAIToolAgent):
    """Same as demo_llm's agent, with configurable temperature (at
    temperature 0 the repetitions would be nearly identical) and with
    parallel calls off by default: the prompt asks to create the customer
    BEFORE invoicing, and in parallel the model invents the identifier."""

    def __init__(self, model=None, temperatura=1.0, paralelas=False,
                 esfuerzo=None, base_url=None):
        super().__init__(model=model, base_url=base_url)
        original = self._client.chat.completions.create

        def create(**kw):
            kw["temperature"] = temperatura
            if not paralelas:
                kw["parallel_tool_calls"] = False
            if esfuerzo:
                kw["reasoning_effort"] = esfuerzo
            return original(**kw)

        self._client.chat.completions.create = create
        if self._client.chat.completions.create is not create:
            raise RuntimeError(
                "could not set the temperature (incompatible openai "
                "version): stopping before spending")


def _cerrar(t, inner, failed_tool="billing.create_invoice"):
    """What whoever integrates Vexinter does after the agent finishes."""
    v = t.verify()
    if v.status == "not_met":
        t.recover(failed_tool=failed_tool)
        v = t.verify()
    elif v.status == "inconclusive":
        if hasattr(inner, "tick"):
            inner.tick()               # simulator resource (see limits)
        for k in range(1, 4):
            r = t.run_followups(at=now() + timedelta(seconds=61 * k))
            if r:
                v = r[-1]
                if v.status != "inconclusive":
                    break
    return v


def ejecutar(llm, clase, brazo, i, verificar=False, aprobacion="auto",
             sondeo="correcto", una_vez=False):
    con_vexinter, con_lectura = BRAZOS[brazo]
    email = f"llm-{clase}-{brazo}-{i}@example.com".replace("+", "_")
    crm, inner = CrmSystem(), FALLAS[clase]()
    inner._aprobacion = aprobacion
    inner._sondeo_ok = (sondeo == "correcto")
    inner._una_vez = una_vez
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
            afirma = v.status == "met"       # what is reported to the operator
            escalado = len(t.pending_cases()) > 0
    else:
        rep = llm.run(_Directa([crm, billing]), objetivo, contexto,
                      allowed=allowed)
        agente = afirma = rep.done is True      # what the agent claims
    logrado, emitidas = oraculo(crm, inner, email)
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


def imprimir_tabla(tabla, clases):
    print(f"{'class':<18}{'arm':<14}{'n':>4}"
          + "".join(f"{c:>13}" for c in COLS))
    suma = {b: dict.fromkeys(COLS + ("n",), 0) for b in BRAZOS}
    for clase in clases:
        for brazo in BRAZOS:
            f = tabla[(clase, brazo)]
            for c in COLS + ("n",):
                suma[brazo][c] += f[c]
            print(f"{clase:<18}{brazo:<14}{f['n']:>4}"
                  + "".join(f"{f[c]:>13}" for c in COLS))
        print()
    print("TOTAL")
    for brazo in BRAZOS:
        f = suma[brazo]
        print(f"{'':<18}{brazo:<14}{f['n']:>4}"
              + "".join(f"{f[c]:>13}" for c in COLS))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=5,
                    help="repetitions per class and arm")
    ap.add_argument("--clases", default=",".join(FALLAS),
                    help="comma-separated list")
    ap.add_argument("--model", help="default: gpt-4o-mini "
                    "(or VEXINTER_LLM_MODEL)")
    ap.add_argument("--temperatura", type=float, default=1.0)
    ap.add_argument("--esfuerzo", choices=("minimal", "low", "medium", "high"),
                    help="reasoning_effort, only for reasoning models")
    ap.add_argument("--paralelas", action="store_true",
                    help="allow parallel tool calls "
                         "(reproduces the setup that failed)")
    ap.add_argument("--traza", action="store_true",
                    help="print each call with its arguments")
    ap.add_argument("--verificar", action="store_true",
                    help="the +lee arms are asked to check with the "
                         "read tool before considering the task "
                         "finished")
    ap.add_argument("--aprobacion", choices=("auto", "human"), default="auto",
                    help="retry policy for writes without "
                         "idempotency: 'human' requires human approval")
    ap.add_argument("--sondeo", choices=("correcto", "defectuoso"),
                    default="correcto",
                    help="'defectuoso' simulates a badly defined effect probe "
                         "(counts something that never changes)")
    ap.add_argument("--una-vez", action="store_true",
                    help="billing declares 'once per execution': an "
                         "identical retry after ok=True returns what was "
                         "already recorded")
    ap.add_argument("--max-entrada", type=int, default=1_500_000,
                    help="hard cap on input tokens")
    ap.add_argument("--max-salida", type=int, default=150_000,
                    help="hard cap on output tokens")
    ap.add_argument("--base-url",
                    help="OpenAI-compatible endpoint (e.g. LM Studio "
                         "http://localhost:1234/v1); also OPENAI_BASE_URL")
    a = ap.parse_args()

    clases = [c.strip() for c in a.clases.split(",") if c.strip()]
    for c in clases:
        if c not in FALLAS:
            sys.exit(f"unknown class: {c}. Valid: {', '.join(FALLAS)}")
    base_url = a.base_url or os.environ.get("OPENAI_BASE_URL")
    if not os.environ.get("OPENAI_API_KEY") and not base_url:
        sys.exit('Missing OPENAI_API_KEY: $env:OPENAI_API_KEY = "sk-..." '
                 '(or set OPENAI_BASE_URL / --base-url for a local server)')

    llm = _Agente(model=a.model, temperatura=a.temperatura,
                  paralelas=a.paralelas, esfuerzo=a.esfuerzo,
                  base_url=base_url)
    print(f"model={llm._model} temperature={a.temperatura} "
          f"parallel_calls={a.paralelas} approval={a.aprobacion} "
          f"probe={a.sondeo} once={a.una_vez} effort={a.esfuerzo}\n")
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
                    fila, linea, traza = ejecutar(llm, clase, brazo, i,
                                                  a.verificar, a.aprobacion,
                                                  a.sondeo, a.una_vez)
                except Exception as e:
                    fila = dict.fromkeys(COLS, 0)
                    fila["errores"] = 1
                    linea = f"ERROR {type(e).__name__}: {str(e)[:120]}"
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
        print("TOKEN CAP REACHED: the run stopped before finishing "
              "(the n column shows how many repetitions were "
              "completed per cell).\n")
    imprimir_tabla(tabla, clases)
    print(f"\ntokens: input={llm.usage['prompt']}, "
          f"output={llm.usage['completion']}")
    tarifas = {"gpt-4o-mini": (0.15, 0.60), "gpt-4.1-mini": (0.40, 1.60),
               "gpt-5-mini": (0.25, 2.00)}
    tarifa = next((v for k, v in tarifas.items()
                   if llm._model.startswith(k)), None)
    if tarifa:
        costo = (llm.usage["prompt"] * tarifa[0]
                 + llm.usage["completion"] * tarifa[1]) / 1_000_000
        print(f"estimated cost: ~US$ {costo:.2f} (list price; "
              "confirm it on your usage dashboard)")


if __name__ == "__main__":
    main()
