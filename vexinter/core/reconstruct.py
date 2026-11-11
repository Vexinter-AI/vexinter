"""Vexinter server (§12.2/§12.3) and reconstruction after restart (§15).

Principle: the persistent log is the only source of truth. On startup,
identities, tasks, pending cases and reviews are rebuilt from events;
nothing else is duplicated into tables.
"""
from __future__ import annotations

from datetime import datetime

from ..identity import IdentityService
from .conditions import canonical_json
from .events import Event, EventType, now
from .runtime import LocalRuntime
from .store import EventQuery
from .taskrun import TaskRun
from .tools import Observation


class ServerRuntime(LocalRuntime):
    """Service composition: persistent log + reconstructed state."""

    MAX_FOLLOWUP_ATTEMPTS = 3

    def __init__(self, adapters, store, *, followup_delay_s: float = 30.0,
                 authenticated: bool = True,
                 credential_ttl_s: float = 3600.0,
                 rotation_grace_s: float = 900.0):
        identity = IdentityService.rebuild(store, credential_ttl_s,
                                           rotation_grace_s)
        super().__init__(adapters, store=store, identity_service=identity,
                         authenticated=authenticated,
                         followup_delay_s=followup_delay_s)
        self.last_verdicts: dict[str, object] = {}
        self.memory.restore_from_log()
        runs = self._restore_runs()
        self.human_queue.restore(runs)
        self._restore_followups(runs)

    def _restore_runs(self) -> dict[str, TaskRun]:
        """Each task's owner comes from the TASK_CREATED event itself
        (agent_id). Tasks created before registration have no owner: only
        the administration plane can reach them."""
        runs: dict[str, TaskRun] = {}
        for e in self.store.query(EventQuery(
                types={EventType.TASK_CREATED}, limit=1_000_000)):
            p = e.payload or {}
            if p.get("_redacted") or p.get("_redactado"):
                continue          
            run = TaskRun(task_id=e.task_id, execution_id=e.execution_id,
                          agent_id=e.agent_id or "",
                          objective=p.get("objective", ""),
                          condition=p.get("condition") or {},
                          input=p.get("input") or {},
                          instance_id=p.get("instance_id"),
                          credential_id=p.get("credential_id"))
            runs[run.task_id] = run
            self.runs[run.task_id] = run
        for e in self.store.query(EventQuery(
                types={EventType.STATE_OBSERVED}, limit=1_000_000)):
            p = e.payload or {}
            if p.get("tag") != "pre" or e.task_id not in runs:
                continue
            key = f"{p.get('system')}|{canonical_json(p.get('query') or {})}"
            runs[e.task_id].pre_snapshot[key] = Observation(
                data=p.get("observed"), observed_at=e.ts,
                source=str(p.get("system")),
                consistency=p.get("consistency", "strong"))
        for e in self.store.query(EventQuery(
                types={EventType.TASK_ASSIGNED}, limit=1_000_000)):
            p = e.payload or {}
            assigned = runs.get(e.task_id or "")
            if (assigned is not None and p.get("collaborator")
                    and e.agent_id == assigned.agent_id
                    and p["collaborator"] not in assigned.collaborators):
                assigned.collaborators.append(p["collaborator"])
        por_exec = {r.execution_id: r for r in runs.values()}
        for e in self.store.query(EventQuery(
                types={EventType.EXECUTION_COMPLETED}, limit=1_000_000)):
            r = por_exec.get(e.execution_id or "")
            if r is not None:
                r.completed = True
        return runs

    def _restore_followups(self, runs: dict[str, TaskRun]) -> None:
        """One pending review per execution: the last scheduled one, unless
        it already finished with 'met' or exhausted its attempts since then."""
        by_exec = {r.execution_id: r for r in runs.values()}
        agendadas: dict[str, Event] = {}
        for e in self.store.query(EventQuery(
                types={EventType.FOLLOWUP_SCHEDULED}, limit=1_000_000)):
            if e.execution_id:
                agendadas[e.execution_id] = e
        ejecutadas: dict[str, list[Event]] = {}
        for e in self.store.query(EventQuery(
                types={EventType.FOLLOWUP_EXECUTED}, limit=1_000_000)):
            if e.execution_id:
                ejecutadas.setdefault(e.execution_id, []).append(e)
        for eid, ev in agendadas.items():
            run = by_exec.get(eid)
            if run is None:
                continue
            hechas = [x for x in ejecutadas.get(eid, []) if x.ts >= ev.ts]
            n = len(hechas)
            if n >= self.MAX_FOLLOWUP_ATTEMPTS:
                continue
            if hechas and (hechas[-1].payload or {}).get("status") == "met":
                continue
            p = ev.payload or {}
            due = (now() if n else datetime.fromisoformat(
                p.get("due_at", now().isoformat())))
            self.scheduler.restore(run, p.get("reason", ""), due, n)