from __future__ import annotations

from .events import Event, EventType


def _label(e: Event) -> str:
    p = e.payload or {}
    for k in ("tool", "to", "status", "case_id", "strategy"):
        if k in p:
            return f"{e.type.value}:{p[k]}"
    return e.type.value


def execution_graph(events: list[Event]) -> dict:
    """Nodos = acontecimientos; aristas tipadas (§3.4):
    'se_origino_en', 'depende_de', 'fue_verificado_por', 'compenso_a'."""
    # Index verdicts by execution once: the per-event scan below was O(N*M).
    verdicts_by_exec: dict[str, list[Event]] = {}
    for v in events:
        if v.type == EventType.VERIFIER_VERDICT:
            verdicts_by_exec.setdefault(v.execution_id or "", []).append(v)
    nodes, edges = [], []
    for e in events:
        nodes.append({"id": e.id, "type": e.type.value,
                      "ts": e.ts.isoformat(), "label": _label(e)})
        if e.caused_by:
            edges.append({"source": e.caused_by, "target": e.id,
                          "rel": "se_origino_en"})
        for d in e.depends_on:
            edges.append({"source": d, "target": e.id, "rel": "depende_de"})
        if e.compensates:
            edges.append({"source": e.compensates, "target": e.id,
                          "rel": "compenso_a"})
        if e.type == EventType.TOOL_RETURNED:
            for v in verdicts_by_exec.get(e.execution_id or "", []):
                edges.append({"source": e.id, "target": v.id,
                              "rel": "fue_verificado_por"})
    return {"nodes": nodes, "edges": edges}