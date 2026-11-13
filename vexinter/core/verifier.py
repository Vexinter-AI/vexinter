from __future__ import annotations

from pydantic import BaseModel

from .conditions import (
    EvalContext,
    NodeResult,
    Outcome,
    eval_condition,
    validate_condition,
)
from .crypto import sha256_hex
from .events import EventDraft, EventType, now
from .tools import SourceRouter


class VerdictResult(BaseModel):
    status: Outcome
    task_id: str
    execution_id: str
    checked_at: object = None
    provisional: bool = False
    followup_required: bool = False
    detail: str | None = None
    tree: NodeResult | None = None
    event_id: str | None = None


class Verifier:
    """Layer B (§4): contrasts the agent's claims with real systems.

    It does not accept the tool's response as proof: it queries the
    sources through `observe` (§4.1).
    """

    def __init__(self, store, router: SourceRouter):
        self._store = store
        self._router = router

    def verify(self, run, condition: dict | None = None,
               record: bool = True,
               extra_bindings: dict | None = None,
               bindings: dict | None = None) -> VerdictResult:
        """§4: evaluates the condition against the real systems.

        `extra_bindings` (legacy alias: `bindings`) allows injecting an
        additional scope — e.g. {"steps": done} for the post-recovery
        invariants of layer E.
        """

        cond = condition if condition is not None else run.condition
        validate_condition(cond)
        ctx = EvalContext(run.input, run.pre_snapshot)
        merged: dict = dict(bindings or {})
        if extra_bindings:
            merged.update(extra_bindings)
        if merged:
            ctx.bindings.update(merged)
            ctx.claimed.update(merged)   
        root = eval_condition(cond, ctx, self._router)
        return self.from_node(
            run, root,
            scope="post_recovery" if condition is not None else "task",
            record=record)

    def from_node(self, run, root: NodeResult, scope: str = "task",
                  record: bool = True) -> VerdictResult:
        """Turns an already-computed evaluation result into a verdict and,
        if `record`, logs it as evidence (§4.3: uncertainty is preserved
        in the log, not only in memory)."""
        v = VerdictResult(
            status=root.outcome, task_id=run.task_id, execution_id=run.execution_id,
            checked_at=now(), provisional=root.provisional,
            followup_required=root.followup, detail=root.detail, tree=root)
        if record:
            ev = self._store.append(EventDraft(
                type=EventType.VERIFIER_VERDICT, task_id=run.task_id,
                execution_id=run.execution_id,
                payload={"status": v.status, "provisional": v.provisional,
                         "followup_required": v.followup_required,
                         "scope": scope,
                         "checks": root.flatten(),
                         # Deterministic per-execution salt: the stored
                         # observed_hash stays recomputable post-mortem
                         # (a random salt was never persisted, making the
                         # hashes unverifiable) while still differing
                         # across executions.
                         "evidence": root.evidence_summary(
                             privacy_salt=sha256_hex(
                                 f"vexinter-evidence|{run.execution_id}"))}),
                stream=run.stream)
            v.event_id = ev.id
        return v