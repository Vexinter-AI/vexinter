from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

from .conditions import EvalContext, eval_condition
from .coordinated import CoordinatedRecovery, PlanFailureOutcome, resolve_path
from .statemachine import ExecutionState, state_from_verdict
from .tools import IndeterminateOutcome, SourceRouter


class CompensatingSpec(BaseModel):
    tool: str
    args_from_result: dict[str, str] = Field(default_factory=dict)


class Step(BaseModel):
    id: str
    tool: str
    args: dict = Field(default_factory=dict)
    args_from: dict[str, str] = Field(default_factory=dict)   
    depends_on: list[str] = Field(default_factory=list)
    postcondition: dict | None = None
    compensation: CompensatingSpec | None = None


class Plan(BaseModel):
    steps: list[Step]
    post_recovery_condition: dict | None = None  


def _topo(steps: list[Step]) -> list[Step]:
    by = {s.id: s for s in steps}
    indeg = {s.id: 0 for s in steps}
    for s in steps:
        for d in dict.fromkeys(s.depends_on):   # dedupe: A,A counts once
            if d not in by:
                raise ValueError(f"unknown dependency: {d}")
            indeg[s.id] += 1
    q = deque([i for i, d in indeg.items() if d == 0])
    out: list[Step] = []
    while q:
        i = q.popleft()
        out.append(by[i])
        for s in steps:
            if i in s.depends_on:
                indeg[s.id] -= 1
                if indeg[s.id] == 0:
                    q.append(s.id)
    if len(out) != len(steps):
        raise ValueError("the plan contains cycles")
    return out


class Coordinator:
    """Layer E (§7): plans with dependencies between systems.

    Does not assume global transactions (§7.4): each step goes through the
    intermediary, its postcondition is verified, and partial failures are
    routed to coordinated recovery (§7.3).
    """

    def __init__(self, intermediary, verifier, router: SourceRouter,
                 coordinated: CoordinatedRecovery, tracker):
        self._int, self._verifier = intermediary, verifier
        self._router, self._coord, self._tracker = router, coordinated, tracker

    def run(self, plan: Plan, run):
        order = _topo(plan.steps)
        done: dict[str, dict] = {}
        ctx = EvalContext(run.input, run.pre_snapshot)
        ctx.bindings["steps"] = done

        final_outcome: PlanFailureOutcome | None = None
        inconclusive = None
        i = 0
        while i < len(order):
            step = order[i]
            args = {**step.args}
            for arg, path in step.args_from.items():
                args[arg] = resolve_path(done, path)
            deps = [done[d]["returned_event"]
                    for d in step.depends_on if d in done]
            try:
                res = self._int.call(run.call_context(), step.tool, args,
                                     depends_on=deps)
            except IndeterminateOutcome as e:
                final_outcome = self._coord.handle_indeterminate(
                    run, done, step, e)
                break
            done[step.id] = {"data": res.data, "ok": res.ok,
                             "invoked_event": res.provenance.invoked_event,
                             "returned_event": res.provenance.returned_event}

            post = (eval_condition(step.postcondition, ctx, self._router)
                    if step.postcondition else None)

            if post is not None and post.outcome == "inconclusive":
                if self._tracker.can(run.execution_id, ExecutionState.INCONCLUSIVE):
                    self._tracker.transition(
                        run, ExecutionState.INCONCLUSIVE,
                        f"postcondition of '{step.id}' inconclusive")
                inconclusive = self._verifier.from_node(
                    run, post, scope="step_postcondition")
                break

            if (not res.ok) or (post is not None and post.outcome != "met"):
                outcome = self._coord.handle_plan_failure(
                    run, plan, order, done, step, res)
                if not outcome.resumed:
                    final_outcome = outcome
                    break
            i += 1

        if i == len(order):                     
            v = self._verifier.verify(run)
            target = state_from_verdict(v.status, v.provisional)
            if self._tracker.can(run.execution_id, target):
                self._tracker.transition(run, target,
                                         f"final verdict: {v.status}")
            return v
        if inconclusive is not None:
            return inconclusive
        return final_outcome.verdict if final_outcome else None