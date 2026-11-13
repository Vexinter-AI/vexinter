from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from .conditions import Outcome
from .events import EventDraft, EventType, new_id, now
from .statemachine import ExecutionState, state_from_verdict
from .store import EventQuery
from .taskrun import TaskRun
from .tools import (
    AmbiguousInvocationError,
    AuthorizationError,
    CallContext,
    IndeterminateOutcome,
    Intermediary,
    OperationClass,
    UnknownToolError,
)

# Per-execution event scans are bounded by design (budgets,
# loop detection); this is a defensive cap, not a table scan.
_EVENT_SCAN_LIMIT = 100_000

class StateError(Exception): ...


class RecoveryPolicy(BaseModel):
    operation_class: OperationClass
    allowed: list[str]
    max_retries: int


DEFAULT_POLICIES: dict[OperationClass, RecoveryPolicy] = {
    OperationClass.READ: RecoveryPolicy(
        operation_class=OperationClass.READ,
        allowed=["retry", "reverify"], max_retries=2),
    OperationClass.IDEMPOTENT_WRITE: RecoveryPolicy(
        operation_class=OperationClass.IDEMPOTENT_WRITE,
        allowed=["retry", "reverify", "human"], max_retries=1),
    OperationClass.REVERSIBLE_WRITE: RecoveryPolicy(
        operation_class=OperationClass.REVERSIBLE_WRITE,
        allowed=["retry", "reverify", "compensate", "human"], max_retries=1),
    OperationClass.EFFECTFUL_IRREVERSIBLE: RecoveryPolicy(
        operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE,
        allowed=["reverify", "human"], max_retries=0),
}


class CaseStatus(str, Enum):
    OPEN = "open"
    WAITING_HUMAN = "waiting_human"
    RESOLVED = "resolved"


class RecoveryCase(BaseModel):
    id: str = Field(default_factory=lambda: new_id("case"))
    task_id: str
    execution_id: str
    agent_id: str | None = None
    tool: str | None = None
    verdict_status: Outcome
    created_at: datetime = Field(default_factory=now)
    status: CaseStatus = CaseStatus.OPEN
    attempts: int = 0
    history: list[str] = Field(default_factory=list)
    invoked_event: str | None = None


class CaseOption(BaseModel):
    action: str
    description: str = ""
    risk: Literal["bajo", "medio", "alto"] = "medio"


_DESCRIPCIONES = {
    "retry": "retry the operation (with idempotency key if it exists)",
    "reverify": "re-verify the current state",
    "compensate": "execute an authorized compensation (requires a compensating tool)",
}


def find_invocation(store, run: TaskRun, tool: str,
                    invoked_event: str | None = None):
    """TOOL_INVOKED event of an operation (§3.4).

    - With `invoked_event`: exactly that invocation.
    - Without it: the last one, but only if all invocations of the tool
      belong to the SAME logical operation (same idempotency key). If there
      are several distinct operations, it is ambiguous which operation
      failed and LookupError is raised instead of guessing.
    """
    evs = store.query(EventQuery(execution_id=run.execution_id,
                                 types={EventType.TOOL_INVOKED},
                                 limit=_EVENT_SCAN_LIMIT))
    if invoked_event is not None:
        for ev in evs:
            if ev.id == invoked_event:
                if not ev.payload or ev.payload.get("tool") != tool:
                    raise LookupError(
                        f"invocation {invoked_event} does not belong to {tool}")
                return ev
        raise LookupError(f"invocation not found: {invoked_event}")

    mine = [ev for ev in evs
            if ev.payload and ev.payload.get("tool") == tool]
    if not mine:
        raise LookupError(f"no prior invocation of {tool}")
    logicas = {ev.payload.get("idempotency_key") or ev.id for ev in mine}
    if len(logicas) > 1:
        raise LookupError(
            f"ambiguous: {len(logicas)} distinct operations of {tool}; "
            "specify which one failed with invoked_event")
    return mine[-1]


def last_invocation(store, run: TaskRun, tool: str,
                    invoked_event: str | None = None) -> dict:
    """Payload of the invocation (see find_invocation)."""
    return find_invocation(store, run, tool, invoked_event).payload


def compensation_for(store, intermediary, run: TaskRun, tool: str | None,
                     invoked_event: str | None = None):
    """§7.3: can the operation of this case be compensated?

    Returns (compensating tool, arguments, invocation event) or None. It is
    only executable if the tool declares its compensation AND the invocation
    left a registered result with the necessary data: an invocation without
    a return (network cut) has no id to undo.
    """
    if not tool:
        return None
    try:
        comp = intermediary.spec_of(tool).compensation
        inv = find_invocation(store, run, tool, invoked_event)
    except (UnknownToolError, LookupError):
        return None
    if comp is None:
        return None
    ret = None
    for ev in store.query(EventQuery(execution_id=run.execution_id,
                                     types={EventType.TOOL_RETURNED},
                                     limit=_EVENT_SCAN_LIMIT)):
        if ev.operation_id == inv.operation_id:
            ret = ev
    data = (ret.payload or {}).get("data") if ret is not None else None
    if not isinstance(data, dict):
        return None
    try:
        args = {k: data[v] for k, v in comp.args_from_result.items()}
    except KeyError:
        return None
    return comp.tool, args, inv


def invocation_context(run: TaskRun, invocation_payload: dict) -> CallContext:
    """Rebuilds the context of the ORIGINAL invocation (§14).

    Retrying an operation must run under the authorization of whoever
    invoked it, not under the task owner's.
    """
    by = (invocation_payload or {}).get("invoked_by") or {}
    return CallContext(
        agent_id=by.get("agent_id") or run.agent_id,
        instance_id=by.get("instance_id"),
        credential_id=by.get("credential_id"),
        task_id=run.task_id, execution_id=run.execution_id,
        stream=run.stream)


class HumanDecisionQueue:
    """Critical rule: before executing a decision the state is RE-VERIFIED,
    because it may have changed while the case waited (§5.4)."""

    def __init__(self, store, verifier, intermediary: Intermediary,
                 tracker=None):
        self._store, self._verifier, self._int = store, verifier, intermediary
        self._tracker = tracker
        self._cases: dict[str, RecoveryCase] = {}
        self._runs: dict[str, TaskRun] = {}
        self._options: dict[str, list[CaseOption]] = {}

    def enqueue(self, run, case, allowed_actions, context: dict | None = None):
        options = [CaseOption(action=a, description=_DESCRIPCIONES.get(a, ""))
                   for a in allowed_actions]
        options.append(CaseOption(action="close",
                                  description="close the case without action",
                                  risk="alto"))
        case.status = CaseStatus.WAITING_HUMAN
        self._cases[case.id] = case
        self._runs[run.task_id] = run
        self._options[case.id] = options
        ev = self._store.append(EventDraft(
            type=EventType.HUMAN_DECISION_REQUESTED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"case_id": case.id,
                     "options": [o.model_dump() for o in options],
                     "context": context or {},
                     "case": case.model_dump(mode="json")}),   
            stream=run.stream)
        
        # Emit HITL_REQUESTED (Fase 2)
        self._store.append(EventDraft(
            type=EventType.HITL_REQUESTED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"case_id": case.id, "tool": case.tool}),
            stream=run.stream)

        case.history.append(ev.id)
        return case

    def pending(self):
        return [c for c in self._cases.values()
                if c.status == CaseStatus.WAITING_HUMAN]

    def options(self, case_id):
        return self._options.get(case_id, [])

    def get_case(self, case_id):
        return self._cases.get(case_id)

    def options_for(self, case_id):
        return self._options.get(case_id, [])

    def decide(self, case_id, action, operator="operator"):
        if case_id not in self._cases:
            raise KeyError(case_id)
        case = self._cases[case_id]
        run = self._runs.get(case.task_id)
        if run is None:
            raise StateError(
                f"task {case.task_id} is no longer available (erased or not rebuilt)"
            )
        if case.status != CaseStatus.WAITING_HUMAN:
            raise StateError("the case is not pending a decision")
        if action not in {o.action for o in self._options[case_id]}:
            raise StateError(f"action not allowed for this case: {action}")

        fresh = self._verifier.verify(run)
        if fresh.event_id:
            case.history.append(fresh.event_id)
        if fresh.status == "met":
            case.status = CaseStatus.RESOLVED
            self._log(run, case, EventType.HUMAN_DECISION_MADE,
                      {"action": action, "operator": operator, "executed": False,
                       "note": "state already complies: action was not executed"})
            self._log(run, case, EventType.CASE_RESOLVED,
                      {"reason": "state changed while the case was waiting"})
            if self._tracker is not None:
                self._tracker.transition(
                    run, state_from_verdict(fresh.status),
                    "state changed while the case was waiting (§5.4)")
            return fresh

        if action == "close":
            case.status = CaseStatus.RESOLVED
            self._log(run, case, EventType.HUMAN_DECISION_MADE,
                      {"action": "close", "operator": operator, "executed": True})
            self._log(run, case, EventType.CASE_RESOLVED,
                      {"reason": "closed without action by human decision"})
            if self._tracker is not None:
                self._tracker.transition(run, ExecutionState.FAILED,
                                        "closed without action")
            return fresh

        if action == "retry":
            inv = last_invocation(self._store, run, case.tool,
                                  invoked_event=case.invoked_event)
            self._log(run, case, EventType.HUMAN_DECISION_MADE,
                      {"action": "retry", "operator": operator, "executed": True})
            self._log(run, case, EventType.RECOVERY_EXECUTED,
                      {"tool": case.tool, "via": "human_decision"})
            self._int.call(invocation_context(run, inv), case.tool, inv["args"],
                           idempotency_key=inv.get("idempotency_key"),
                           retry_authorized=True)
            fresh = self._verifier.verify(run)
            if fresh.status == "met":
                case.status = CaseStatus.RESOLVED
                self._log(run, case, EventType.CASE_RESOLVED,
                          {"reason": "manual retry conforms"})
                if self._tracker is not None:
                    self._tracker.transition(
                        run, state_from_verdict(fresh.status),
                        "retry authorized by human decision")
            return fresh

        if action == "reverify":
            self._log(run, case, EventType.HUMAN_DECISION_MADE,
                      {"action": "reverify", "operator": operator})
            return fresh

        if action == "compensate":
            plan = compensation_for(self._store, self._int, run, case.tool,
                                    case.invoked_event)
            if plan is None:
                raise StateError("compensation is not executable for this "
                                 "case: missing compensating tool or a "
                                 "registered result")
            comp_tool, comp_args, inv_ev = plan
            self._log(run, case, EventType.HUMAN_DECISION_MADE,
                      {"action": "compensate", "operator": operator,
                       "executed": True})
            res = self._int.call(run.call_context(), comp_tool, comp_args)
            extra = {"compensates": inv_ev.id} if res.ok else {}
            ev = self._store.append(EventDraft(
                type=EventType.RECOVERY_EXECUTED, task_id=run.task_id,
                execution_id=run.execution_id, **extra,
                payload={"tool": comp_tool, "via": "human_decision",
                         "compensates_tool": case.tool, "ok": res.ok}),
                stream=run.stream)
            case.history.append(ev.id)
            if not res.ok:
                return fresh          
            fresh = self._verifier.verify(run)
            case.status = CaseStatus.RESOLVED
            self._log(run, case, EventType.CASE_RESOLVED,
                      {"reason": "manual compensation executed"})
            if self._tracker is not None:
                target = state_from_verdict(fresh.status, fresh.provisional)
                if self._tracker.can(run.execution_id, target):
                    self._tracker.transition(
                        run, target, "compensation authorized by human decision")
            return fresh

        raise StateError(f"action not implemented: {action}")

    def restore(self, runs_by_task):
        """§15: rebuilds pending cases from the log."""
        requested = {}
        for e in self._store.query(EventQuery(
                types={EventType.HUMAN_DECISION_REQUESTED}, limit=_EVENT_SCAN_LIMIT)):
            p = e.payload or {}
            requested[p.get("case_id", "")] = (p.get("case") or {},
                                               p.get("options") or [],
                                               e.task_id or "")
        resolved = set()
        for e in self._store.query(EventQuery(
                types={EventType.CASE_RESOLVED}, limit=_EVENT_SCAN_LIMIT)):
            if e.payload:
                resolved.add(e.payload.get("case_id"))
        for cid, (case_d, options_d, task_id) in requested.items():
            if cid in resolved or task_id not in runs_by_task:
                continue
            self._cases[cid] = RecoveryCase(**case_d)
            self._runs[task_id] = runs_by_task[task_id]
            self._options[cid] = [CaseOption(**o) for o in options_d]

    def _log(self, run, case, etype: EventType, payload: dict):
        ev = self._store.append(EventDraft(
            type=etype, task_id=run.task_id, execution_id=run.execution_id,
            payload={"case_id": case.id, **payload}), stream=run.stream)
        case.history.append(ev.id)


class RecoveryEngine:
    """Turns detected discrepancies into admissible actions (§5).

    It does not automatically repeat any failed operation: it determines
    what is safe, what requires more evidence and what needs human
    authorization.
    """

    def __init__(self, store, verifier, intermediary: Intermediary,
                 human_queue: HumanDecisionQueue, tracker=None,
                 policies: dict[OperationClass, RecoveryPolicy] | None = None):
        self._store, self._verifier = store, verifier
        self._int, self._human = intermediary, human_queue
        self._tracker = tracker
        self._policies = policies or DEFAULT_POLICIES

    def policy_for(self, tool: str) -> RecoveryPolicy | None:
        try:
            spec = self._int.spec_of(tool)
        except UnknownToolError:
            return None
        return self._policies.get(spec.operation_class)

    def handle_discrepancy(self, run, verdict, failed_tool=None,
                           invoked_event=None):
        ev = self._store.append(EventDraft(
            type=EventType.DISCREPANCY_DETECTED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"tool": failed_tool, "verdict": verdict.status}),
            stream=run.stream)
        case = RecoveryCase(task_id=run.task_id, execution_id=run.execution_id,
                            agent_id=run.agent_id, tool=failed_tool,
                            invoked_event=invoked_event,
                            verdict_status=verdict.status)
        case.history.append(ev.id)

        if (self._tracker is not None
                and self._tracker.can(run.execution_id,
                                      ExecutionState.PENDING_RECOVERY)):
            self._tracker.transition(run, ExecutionState.PENDING_RECOVERY,
                                     "discrepancy detected")

        fresh = self._verifier.verify(run)
        if fresh.event_id:
            case.history.append(fresh.event_id)
        if fresh.status == "met":
            return self._resolve(run, case,
                                 "state already complies upon re-verification",
                                 verdict=fresh)
        if fresh.status == "inconclusive":
            return self._escalate(run, case,
                                  "insufficient evidence after re-verification")

        policy = self.policy_for(failed_tool) if failed_tool else None
        aprobacion = False
        if policy is not None:
            spec = self._int.spec_of(failed_tool)
            aprobacion = (not spec.supports_idempotency
                          and spec.operation_class != OperationClass.READ
                          and spec.retry_approval == "human"
                          and "retry" in policy.allowed)
            reintento_seguro = (
                "retry" in policy.allowed
                and case.attempts < policy.max_retries
                and (spec.operation_class == OperationClass.READ
                     or spec.supports_idempotency
                     or self._efecto_descartado(run, failed_tool,
                                                case.invoked_event)))   
            if reintento_seguro:
                self._decide(run, case,
                             f"retry authorized: policy "
                             f"{policy.operation_class.value}, idempotency "
                             f"supported={spec.supports_idempotency}")
                try:
                    v2 = self._retry(run, case, failed_tool)
                except (AuthorizationError, UnknownToolError, LookupError,
                        IndeterminateOutcome, AmbiguousInvocationError) as e:
                    return self._escalate(run, case,
                                          f"retry not admissible: {e}",
                                          sin_retry=True)
                case.attempts += 1
                if v2.status == "met":
                    return self._resolve(
                        run, case,
                        "conforming retry (idempotency key "
                        "reused, §5.2)", verdict=v2)
        ocurrio = bool(failed_tool) and self._efecto_ocurrio(
            run, failed_tool, case.invoked_event)
        if ocurrio:
            reason = ("an independent read confirmed that the effect of the "
                      "invocation already happened: repeating it would duplicate it")
        elif aprobacion:
            reason = ("the retry requires human approval (tool "
                      "policy)")
        else:
            reason = "no automatic action admissible"
        return self._escalate(run, case, reason, sin_retry=ocurrio)

    def _efecto_descartado(self, run, tool, invoked_event) -> bool:
        """Non-idempotent write: it is only retried automatically if the tool
        does not require human approval and an independent read confirms
        that the invocation produced no effect (§4.3)."""
        try:
            if self._int.spec_of(tool).retry_approval == "human":
                return False
        except UnknownToolError:
            return False
        try:
            inv = last_invocation(self._store, run, tool,
                                  invoked_event=invoked_event)
        except LookupError:
            return False
        return self._int.retry_is_provably_safe(tool, inv)

    def _efecto_ocurrio(self, run, tool, invoked_event) -> bool:
        """Does an independent read confirm that the effect already happened?"""
        try:
            inv = last_invocation(self._store, run, tool,
                                  invoked_event=invoked_event)
            return self._int.effect_provably_occurred(tool, inv)
        except (LookupError, KeyError):
            return False

    def _retry(self, run, case, tool):
        inv = last_invocation(self._store, run, tool,
                              invoked_event=case.invoked_event)
        ev = self._store.append(EventDraft(
            type=EventType.RECOVERY_EXECUTED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"tool": tool,
                     "idempotency_key": inv.get("idempotency_key")}),
            stream=run.stream)
        case.history.append(ev.id)
        self._int.call(invocation_context(run, inv), tool, inv["args"],
                       idempotency_key=inv.get("idempotency_key"))
        return self._verifier.verify(run)

    def _decide(self, run, case, reason):
        ev = self._store.append(EventDraft(
            type=EventType.RECOVERY_DECIDED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"case_id": case.id, "reason": reason}), stream=run.stream)
        case.history.append(ev.id)

    def _resolve(self, run, case, reason, verdict=None):
        case.status = CaseStatus.RESOLVED
        ev = self._store.append(EventDraft(
            type=EventType.CASE_RESOLVED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"case_id": case.id, "reason": reason}), stream=run.stream)
        case.history.append(ev.id)
        if self._tracker is not None and verdict is not None:
            target = state_from_verdict(verdict.status, verdict.provisional)
            if self._tracker.can(run.execution_id, target):
                self._tracker.transition(run, target, reason)
        return case

    def _escalate(self, run, case, reason, sin_retry=False):
        policy = self.policy_for(case.tool) if case.tool else None
        allowed = policy.allowed if policy else ["reverify"]
        allowed = [a for a in allowed
                   if a != "human" and not (sin_retry and a == "retry")]
        if "compensate" in allowed and compensation_for(
                self._store, self._int, run, case.tool,
                case.invoked_event) is None:
            allowed = [a for a in allowed if a != "compensate"]
        allowed = allowed or ["reverify"]
        self._decide(run, case, f"escalated to human decision: {reason}")
        case = self._human.enqueue(run, case, allowed)
        if (self._tracker is not None
                and self._tracker.can(run.execution_id,
                                      ExecutionState.PENDING_HUMAN)):
            self._tracker.transition(run, ExecutionState.PENDING_HUMAN, reason)
        return case