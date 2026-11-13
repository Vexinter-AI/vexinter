from __future__ import annotations

from collections import Counter, defaultdict

from .events import Event, EventType
from .statemachine import derive_execution_state
from .store import EventQuery

_METRICS_SCAN_LIMIT = 100_000


def compute_metrics(store) -> dict:
    """§13.5 metrics, derived EXCLUSIVELY from the log.

    Nothing is computed from in-memory counters: if the log is the
    source of truth, the metrics must be too.
    """
    events = store.query(EventQuery(limit=_METRICS_SCAN_LIMIT))
    by_exec: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.execution_id:
            by_exec[e.execution_id].append(e)

    final = Counter(derive_execution_state(evs).value
                      for evs in by_exec.values())

    false_successes = 0
    for evs in by_exec.values():
        tool_success = any(
            e.type == EventType.TOOL_RETURNED and e.payload and e.payload.get("ok")
            for e in evs)
        not_met_verdict = any(
            e.type == EventType.VERIFIER_VERDICT and e.payload
            and e.payload.get("status") == "not_met"
            and e.payload.get("scope", "task") == "task"    
            for e in evs)                                   
        if tool_success and not_met_verdict:
            false_successes += 1

    c = Counter(e.type for e in events)
    
    msg_rejects = Counter(
        (e.payload or {}).get("reason", "unknown") for e in events
        if e.type == EventType.MESSAGE_REJECTED)
    tool_rejects = Counter(
        (e.payload or {}).get("reason", "unknown") for e in events
        if e.type == EventType.TOOL_REJECTED and e.payload)
    return {
        "executions": len(by_exec),
        "final_states": dict(final),
        "false_successes_detected": false_successes,
        "discrepancies": c[EventType.DISCREPANCY_DETECTED],
        "retries_or_compensations": c[EventType.RECOVERY_EXECUTED],
        "human_decisions_requested": c[EventType.HUMAN_DECISION_REQUESTED],
        "human_decisions_made": c[EventType.HUMAN_DECISION_MADE],
        "rejected_operations": c[EventType.TOOL_REJECTED],
        "authentication_errors": msg_rejects.get("invalid_credential", 0),
        "integrity_failures": (msg_rejects.get("bad_signature", 0)
                                 + msg_rejects.get("tampering", 0)),
        "message_rejections_by_reason": dict(msg_rejects),
        "tool_rejections_by_reason": dict(tool_rejects),
    }