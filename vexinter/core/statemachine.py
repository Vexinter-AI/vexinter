from __future__ import annotations

import logging
import threading
from enum import Enum

from .events import EventDraft, EventType
from .store import EventQuery

_log = logging.getLogger(__name__)


class ExecutionState(str, Enum):
    """Operational states of an execution (§13.1)."""
    IN_PROGRESS             = "in_progress"
    PENDING_VERIFICATION    = "pending_verification"
    PROVISIONALLY_SATISFIED = "provisionally_satisfied"
    SATISFIED               = "satisfied"
    FAILED                   = "failed"
    INCONCLUSIVE            = "inconclusive"
    PENDING_RECOVERY         = "pending_recovery"
    PENDING_HUMAN            = "pending_human_decision"


VALID: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.IN_PROGRESS: frozenset({
        ExecutionState.IN_PROGRESS,              
        ExecutionState.PENDING_VERIFICATION,
        ExecutionState.PENDING_RECOVERY,
        ExecutionState.PENDING_HUMAN,
        ExecutionState.SATISFIED,
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.FAILED,
        ExecutionState.INCONCLUSIVE,
    }),
    ExecutionState.PENDING_VERIFICATION: frozenset({
        ExecutionState.SATISFIED,
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.FAILED,
        ExecutionState.INCONCLUSIVE,
        ExecutionState.PENDING_RECOVERY,
    }),
    ExecutionState.PROVISIONALLY_SATISFIED: frozenset({
        ExecutionState.PENDING_VERIFICATION,      
        ExecutionState.SATISFIED,                 
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.FAILED,                     
        ExecutionState.INCONCLUSIVE,
        ExecutionState.PENDING_RECOVERY,
    }),
    ExecutionState.INCONCLUSIVE: frozenset({
        ExecutionState.IN_PROGRESS,                
        ExecutionState.PENDING_VERIFICATION,
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.SATISFIED,
        ExecutionState.FAILED,
        ExecutionState.INCONCLUSIVE,
        ExecutionState.PENDING_RECOVERY,
        ExecutionState.PENDING_HUMAN,              
    }),
    ExecutionState.PENDING_RECOVERY: frozenset({
        ExecutionState.PENDING_RECOVERY,
        ExecutionState.SATISFIED,
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.FAILED,
        ExecutionState.INCONCLUSIVE,
        ExecutionState.PENDING_HUMAN,              
    }),
    ExecutionState.PENDING_HUMAN: frozenset({
        ExecutionState.PENDING_HUMAN,
        ExecutionState.SATISFIED,                  
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.FAILED,                    
        ExecutionState.INCONCLUSIVE,
        ExecutionState.PENDING_RECOVERY,          
    }),
    ExecutionState.SATISFIED: frozenset({
        ExecutionState.SATISFIED,
        ExecutionState.INCONCLUSIVE,
        ExecutionState.FAILED,
        ExecutionState.PENDING_RECOVERY,
    }),
    ExecutionState.FAILED: frozenset({
        ExecutionState.FAILED,
        ExecutionState.SATISFIED,                  
        ExecutionState.PROVISIONALLY_SATISFIED,
        ExecutionState.INCONCLUSIVE,
        ExecutionState.PENDING_RECOVERY,
    }),
}


class StateTransitionError(Exception):
    def __init__(self, current: ExecutionState, target: ExecutionState):
        self.current, self.target = current, target
        super().__init__(f"invalid transition: {current.value} → {target.value}")


def state_from_verdict(status: str, provisional: bool = False) -> ExecutionState:
    """Verdict → state projection (§4.3, §4.4)."""
    if status == "met":
        return (ExecutionState.PROVISIONALLY_SATISFIED if provisional
                else ExecutionState.SATISFIED)
    if status == "not_met":
        return ExecutionState.FAILED
    return ExecutionState.INCONCLUSIVE


def derive_execution_state(events) -> ExecutionState:
    """PURE projection from the log (§3.4, §15).

    The log is the source of truth: after a restart, the state of any
    execution is rebuilt from here, not from memory.
    """
    current = ExecutionState.IN_PROGRESS
    for e in sorted(events, key=lambda x: (x.ts, x.seq)):
        if (e.type == EventType.EXECUTION_STATE_CHANGED
                and e.payload and "to" in e.payload):
            target = ExecutionState(e.payload["to"])
            # The log is append-only and may contain a transition that
            # raced past VALID (multi-writer deployments): an invalid edge
            # must not corrupt the projection — skip it.
            if target in VALID[current]:
                current = target
            else:
                _log.warning(
                    "skipping invalid state transition %s -> %s "
                    "(execution %s): multi-writer race or corrupt log",
                    current.value, target.value,
                    getattr(e, "execution_id", "?"))
    return current


class ExecutionTracker:
    """Applies VALIDATED transitions and records them (§13.1)."""

    def __init__(self, store):
        self._store = store
        self._lock = threading.Lock()

    def current(self, execution_id: str) -> ExecutionState:
        # No cache: with PostgresEventStore another instance may have
        # appended transitions this process never saw.
        evs = self._store.query(EventQuery(execution_id=execution_id, limit=10_000))
        return derive_execution_state(evs)

    def can(self, execution_id: str, target: ExecutionState) -> bool:
        return target in VALID[self.current(execution_id)]

    def transition(self, run, target: ExecutionState, reason: str) -> ExecutionState:
        draft = EventDraft(
            type=EventType.EXECUTION_STATE_CHANGED,
            task_id=run.task_id, execution_id=run.execution_id,
            payload={"to": target.value, "reason": reason})
        # Stores with append_state_transition validate and append inside
        # one atomic unit (store lock / FOR UPDATE txn). The fallback
        # covers third-party stores without that method.
        atomic = getattr(self._store, "append_state_transition", None)
        if atomic is not None:
            atomic(draft, run.stream, run.execution_id, target)
            return target
        with self._lock:
            current = self.current(run.execution_id)
            if target not in VALID[current]:
                raise StateTransitionError(current, target)
            draft.payload = {**(draft.payload or {}), "from": current.value}
            self._store.append(draft, run.stream)
            return target