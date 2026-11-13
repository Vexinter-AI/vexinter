from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel

from .conditions import ConditionError, EvalContext, eval_condition
from .events import EventDraft, EventType
from .recovery import (
    DEFAULT_POLICIES,
    HumanDecisionQueue,
    RecoveryCase,
    invocation_context,
    last_invocation,
)
from .statemachine import ExecutionState, state_from_verdict
from .store import EventQuery
from .taskrun import TaskRun
from .tools import IndeterminateOutcome, Intermediary, OperationClass, SourceRouter
from .verifier import Verifier

_log = logging.getLogger(__name__)

class RecoveryStrategy(str, Enum):
    FORWARD = "forward"     
    BACKWARD = "backward"   
    HUMAN = "human"         


class PlanFailureOutcome(BaseModel):
    strategy: RecoveryStrategy
    resumed: bool = False
    verdict: Any = None
    detail: str | None = None


def resolve_path(root: dict, path: str) -> Any:
    cur = root
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"path unavailable: {path}")
        cur = cur[part]
    return cur


class CoordinatedRecovery:
    """§7.3: coordinated recovery WITHOUT assuming global transactions (§7.4).

    Identity note (§14): RETRIES execute with the identity of the
    original invocation (whoever invoked the step); COMPENSATIONS are
    new operations executed by whoever runs the plan (the task owner).
    """

    def __init__(self, store, intermediary: Intermediary, verifier: Verifier,
                 router: SourceRouter, human_queue: HumanDecisionQueue,
                 tracker, policies=None):
        self._store, self._int = store, intermediary
        self._verifier, self._router = verifier, router
        self._human, self._tracker = human_queue, tracker   
        self._policies = policies or DEFAULT_POLICIES

    def handle_plan_failure(self, run: TaskRun, plan, order, done: dict,
                            failed_step, step_result) -> PlanFailureOutcome:
        self._store.append(EventDraft(
            type=EventType.DISCREPANCY_DETECTED,
            task_id=run.task_id, execution_id=run.execution_id,
            payload={"tool": failed_step.tool, "step": failed_step.id,
                     "ok": step_result.ok}), stream=run.stream)
        if self._tracker.can(run.execution_id, ExecutionState.PENDING_RECOVERY):
            self._tracker.transition(run, ExecutionState.PENDING_RECOVERY,
                                     f"step '{failed_step.id}' failed")

        strategy = self._choose_strategy(run, order, done, failed_step)
        self._store.append(EventDraft(
            type=EventType.RECOVERY_DECIDED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"strategy": strategy.value, "step": failed_step.id}),
            stream=run.stream)

        if strategy is RecoveryStrategy.FORWARD:
            return self._forward(run, plan, order, done, failed_step)
        if strategy is RecoveryStrategy.BACKWARD:
            return self._backward(run, plan, order, done, failed_step)
        return self._escalate_human(
            run, done, failed_step,
            reason="no safe retry or complete compensation "
                    "available (§7.3)")

    def handle_indeterminate(self, run, done, failed_step,
                             detalle) -> PlanFailureOutcome:
        """The step did not give a definitive answer: it is unknown whether
        its effect occurred (§7.2). It is neither retried nor blindly
        compensated: a person decides."""
        self._store.append(EventDraft(
            type=EventType.DISCREPANCY_DETECTED,
            task_id=run.task_id, execution_id=run.execution_id,
            payload={"tool": failed_step.tool, "step": failed_step.id,
                     "ok": None, "indeterminate": True}), stream=run.stream)
        if self._tracker.can(run.execution_id, ExecutionState.PENDING_RECOVERY):
            self._tracker.transition(
                run, ExecutionState.PENDING_RECOVERY,
                f"step '{failed_step.id}' did not give a definitive answer")
        return self._escalate_human(
            run, done, failed_step,
            reason=f"unknown result from '{failed_step.id}' ({detalle}): "
                   "it is unknown whether the effect occurred (§7.2)")

    def _choose_strategy(self, run, order, done, failed_step) -> RecoveryStrategy:
        spec = self._int.spec_of(failed_step.tool)
        policy = self._policies.get(spec.operation_class)
        retries = self._count_retries(run, failed_step, done)

        if (policy is not None and "retry" in policy.allowed
                and spec.operation_class in (OperationClass.READ,
                                             OperationClass.IDEMPOTENT_WRITE)
                and spec.supports_idempotency
                and retries < policy.max_retries):
            return RecoveryStrategy.FORWARD

        previos = [s for s in order if s.id in done and done[s.id].get("ok")]
        if all(s.compensation for s in previos):
            return RecoveryStrategy.BACKWARD
        return RecoveryStrategy.HUMAN

    def _count_retries(self, run, failed_step, done) -> int:
        """Retries already made of THIS logical operation (same idempotency
        key), not of all invocations of the tool."""
        evs = self._store.query(EventQuery(
            execution_id=run.execution_id, types={EventType.TOOL_INVOKED},
            limit=100_000))
        inv_id = (done.get(failed_step.id) or {}).get("invoked_event")
        key = None
        for e in evs:
            if e.id == inv_id:
                key = (e.payload or {}).get("idempotency_key")
                break
        if key is None:
            return sum(1 for e in evs
                       if e.payload
                       and e.payload.get("tool") == failed_step.tool) - 1
        return sum(1 for e in evs
                   if (e.payload or {}).get("idempotency_key") == key) - 1

    def _forward(self, run, plan, order, done, failed_step) -> PlanFailureOutcome:
        inv = last_invocation(
            self._store, run, failed_step.tool,
            invoked_event=(done.get(failed_step.id) or {}).get("invoked_event"))
        try:
            res = self._int.call(
                invocation_context(run, inv), failed_step.tool, inv["args"],
                depends_on=[done[d]["returned_event"]
                            for d in failed_step.depends_on if d in done],
                idempotency_key=inv.get("idempotency_key"))
        except IndeterminateOutcome as e:
            return self.handle_indeterminate(run, done, failed_step, e)
        done[failed_step.id] = {
            "data": res.data, "ok": res.ok,
            "invoked_event": res.provenance.invoked_event,
            "returned_event": res.provenance.returned_event}
        self._store.append(EventDraft(
            type=EventType.RECOVERY_EXECUTED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"tool": failed_step.tool, "via": "forward"}),
            stream=run.stream)

        post = self._eval_step_postcondition(run, order, done, failed_step)
        if res.ok and (post is None or post.outcome == "met"):
            return PlanFailureOutcome(strategy=RecoveryStrategy.FORWARD,
                                      resumed=True,
                                      detail="conforming retry (§5.2)")
        return self.handle_plan_failure(run, plan, order, done, failed_step, res)

    def _backward(self, run, plan, order, done, failed_step) -> PlanFailureOutcome:
        previos = [s for s in order if s.id in done and done[s.id].get("ok")]
        for step in reversed(previos):
            comp = step.compensation
            args = {k: resolve_path(done, f"{step.id}.data.{v}")
                    for k, v in comp.args_from_result.items()}
            res = self._int.call(run.call_context(), comp.tool, args)
            extra = ({"compensates": done[step.id]["invoked_event"]}
                     if res.ok else {})
            ev = self._store.append(EventDraft(
                type=EventType.RECOVERY_EXECUTED,
                task_id=run.task_id, execution_id=run.execution_id,
                **extra,
                payload={"tool": comp.tool, "via": "backward",
                         "compensates_step": step.id, "ok": res.ok}),
                stream=run.stream)
            if not res.ok:
                return self._escalate_human(
                    run, done, failed_step,
                    reason=f"compensation of '{step.id}' failed "
                           f"({comp.tool}: {res.error}); compensation was "
                           "stopped to avoid leaving an inconsistent "
                           "state (§7.3)")
            done[step.id]["compensated"] = True
            done[step.id]["compensation_event"] = ev.id

        if plan.post_recovery_condition is not None:
            try:
                post_v = self._verifier.verify(
                    run, condition=plan.post_recovery_condition,
                    extra_bindings={"steps": done})
            except ConditionError as e:
                return self._escalate_human(
                    run, done, failed_step,
                    reason=f"could not evaluate the post-recovery "
                           f"invariant: {e}")
            if post_v.status != "met":
                return self._escalate_human(
                    run, done, failed_step,
                    reason="post-recovery invariant violated: possible "
                           "ambiguous effect of the failed step (§7.2)")

        v = self._verifier.verify(run)
        target = state_from_verdict(v.status, v.provisional)
        if self._tracker.can(run.execution_id, target):
            self._tracker.transition(run, target, "compensated execution (§7.3)")
        return PlanFailureOutcome(strategy=RecoveryStrategy.BACKWARD,
                                  verdict=v,
                                  detail="previous steps compensated")

    def _escalate_human(self, run, done, failed_step, reason) -> PlanFailureOutcome:
        case = RecoveryCase(task_id=run.task_id, execution_id=run.execution_id,
                            agent_id=run.agent_id, tool=failed_step.tool,
                            verdict_status="not_met")
        if self._tracker.can(run.execution_id, ExecutionState.PENDING_HUMAN):
            self._tracker.transition(run, ExecutionState.PENDING_HUMAN, reason)
        self._human.enqueue(run, case, ["reverify", "close"],
                            context={"reason": reason,
                                     "failed_step": failed_step.id,
                                     "pasos_completados": sorted(done),
                                     "riesgo": "possibly inconsistent state"})
        return PlanFailureOutcome(strategy=RecoveryStrategy.HUMAN, detail=reason)

    def _eval_step_postcondition(self, run, order, done, step):
        if step.postcondition is None:
            return None
        ctx = EvalContext(run.input, run.pre_snapshot)
        ctx.bindings["steps"] = done
        for s in order:
            if s.id in done and done[s.id].get("ok") and s.postcondition:
                try:
                    eval_condition(s.postcondition, ctx, self._router)
                except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
                    _log.warning(
                        "prior step postcondition '%s' failed to evaluate "
                        "(bindings may be incomplete): %s", s.id, e)
        return eval_condition(step.postcondition, ctx, self._router)