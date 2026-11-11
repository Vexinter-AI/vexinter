from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from .events import EventDraft, EventType, now
from .statemachine import state_from_verdict
from .taskrun import TaskRun


@dataclass
class _Pending:
    run: TaskRun
    reason: str
    due_at: datetime
    attempts: int = 0
    max_attempts: int = 3


class FollowupScheduler:
    """Runs follow-up verifications and recovers them after a restart.

    Availability (§15): in production this scheduler is persistent
    (database queue); here it is in-memory with an injectable clock
    so it can be tested.
    """

    def __init__(self, store, verifier, recovery=None, tracker=None):
        self._store, self._verifier = store, verifier
        self._recovery, self._tracker = recovery, tracker
        self._pending: list[_Pending] = []
        self._lock = threading.RLock()

    def schedule(self, run: TaskRun, delay_s: float, reason: str) -> None:
        with self._lock:
            if any(p.run.execution_id == run.execution_id
                   for p in self._pending):
                return
            due = now() + timedelta(seconds=delay_s)
            self._pending.append(
                _Pending(run=run, reason=reason, due_at=due))
        self._store.append(EventDraft(
            type=EventType.FOLLOWUP_SCHEDULED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"reason": reason, "due_at": due.isoformat()}),
            stream=run.stream)

    def run_due(self, at: datetime | None = None):
        with self._lock:
            return self._run_due_locked(at)

    def _run_due_locked(self, at: datetime | None = None):
        t = at or now()
        results, keep = [], []
        for p in self._pending:
            if p.due_at > t:
                keep.append(p)
                continue
            v = self._verifier.verify(p.run)
            if self._tracker is not None:
                target = state_from_verdict(v.status, v.provisional)
                if self._tracker.can(p.run.execution_id, target):
                    self._tracker.transition(p.run, target,
                                             f"follow-up review: {v.status}")
            self._store.append(EventDraft(
                type=EventType.FOLLOWUP_EXECUTED, task_id=p.run.task_id,
                execution_id=p.run.execution_id,
                payload={"reason": p.reason, "status": v.status,
                         "attempt": p.attempts + 1}),
                stream=p.run.stream)
            results.append(v)
            if v.status != "met":
                p.attempts += 1
                if p.attempts < p.max_attempts:
                    p.due_at = t + timedelta(seconds=30)  
                    keep.append(p)
                elif self._recovery is not None:
                    self._recovery.handle_discrepancy(p.run, v, failed_tool=None)
        self._pending = keep
        return results
    
    def restore(self, run, reason: str, due_at, attempts: int) -> None:
        """§15: repopulates a pending review after a restart."""
        self._pending.append(_Pending(run=run, reason=reason,
                                      due_at=due_at, attempts=attempts))