"""Budget models and cost tracking for FinOps (Fase 1)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class BudgetExceededError(Exception):
    """The token or USD budget has been exhausted (§1.1)."""

    def __init__(self, budget_type: str, spent: Decimal, limit: Decimal):
        super().__init__(
            f"Budget exceeded ({budget_type}): spent={spent}, limit={limit}"
        )
        self.budget_type = budget_type
        self.spent = spent
        self.limit = limit


class LoopViolationError(Exception):
    """Loop detected in the tool sequence (§1.2)."""

    def __init__(self, loop_signature: str, suggested_action: str):
        super().__init__()
        self.loop_signature = loop_signature
        self.suggested_action = suggested_action


class Budget(BaseModel):
    """Budget per Task/Execution."""

    max_tokens: int | None = Field(
        default=None, description="Maximum number of allowed tokens"
    )
    max_usd: Decimal | None = Field(
        default=None, description="Maximum allowed cost in USD"
    )
    model_pricing: dict[str, dict[str, Decimal]] = Field(
        default_factory=dict,
        description="Price per model: {model: {'input': $, 'output': $}}",
    )
    loop_threshold: int | None = Field(
        default=None,
        description="Identical tool-call signatures tolerated before a "
                    "loop violation (default: service VEXINTER_LOOP_THRESHOLD "
                    "or 3)",
    )

    def total_limit(self) -> Decimal:
        """Returns the total budget limit in USD."""
        limit = self.max_usd or Decimal(0)
        if self.model_pricing:
            limit = max(limit, Decimal(0))
        return limit

    def exceeded(self, spent_tokens: int, spent_usd: Decimal) -> tuple[bool, str | None]:
        """Checks whether the budget was exceeded."""
        reasons: list[str] = []

        if self.max_tokens is not None and spent_tokens >= self.max_tokens:
            reasons.append(f"tokens_spent={spent_tokens} >= max_tokens={self.max_tokens}")

        if self.max_usd is not None and spent_usd >= self.max_usd:
            reasons.append(
                f"cost_spent=${spent_usd:.4f} >= max_usd=${self.max_usd:.4f}"
            )

        return (len(reasons) > 0, "; ".join(reasons) if reasons else None)


@dataclass
class CostMetric:
    """Cost metric per tool."""

    tool: str
    model: str | None
    tokens_used: int
    cost_usd: Decimal

    @classmethod
    def from_result(
        cls, result: Any, default_model: str | None
    ) -> CostMetric:
        """
        Extracts cost metrics from a ToolResult.

        Args:
            result: ToolResult.data object that may contain `usage`
            default_model: Default model if not in usage

        Returns:
            CostMetric with normalized metrics
        """
        data = result if isinstance(result, dict) else {}

        # Buscar usage standard (OpenAI: tokens.prompt + tokens.completion)
        tokens_used = data.get("tokens", {}).get("total", 0)
        if tokens_used == 0:
            # Fallback to per-model tokens (metadata)
            tokens_used = data.get("tokens_used", 0)
        if tokens_used == 0:
            # Approximate calculation
            prompt = data.get("prompt", "")
            completion = data.get("completion", "")
            tokens_used = len(prompt.split()) + len(completion.split())

        # Buscar costo ( OpenAI: cost, cost_per_token)
        cost_usd = data.get("cost_usd") or data.get("cost_per_token", 0)

        model_used = data.get("model", default_model)

        return cls(
            tool=data.get("tool", "unknown"),
            model=model_used,
            tokens_used=tokens_used,
            cost_usd=Decimal(str(cost_usd)),
        )


@dataclass
class ExecutionBudgetTracker:
    """Budget tracker for an execution."""

    execution_id: str
    start_time: float  # epoch timestamp
    budget: Budget
    metrics: list[CostMetric] = field(default_factory=list)
    loop_signatures: list[str] = field(default_factory=list)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False)
    loop_threshold: int = 3
    last_loop_check_idx: int = -1

    @property
    def elapsed_seconds(self) -> float:
        """Time elapsed since start."""
        import time

        return time.time() - self.start_time

    @property
    def total_tokens(self) -> int:
        """Total accumulated tokens."""
        with self._lock:
            return sum(m.tokens_used for m in self.metrics)

    @property
    def total_cost(self) -> Decimal:
        """Total accumulated cost."""
        with self._lock:
            return sum((m.cost_usd for m in self.metrics), Decimal(0))

    def add_metric(self, result: Any, default_model: str | None = None):
        """
        Adds a cost measurement for this execution.

        Args:
            result: Result object from tool invocation
            default_model: Default model for approximate costs
        """
        metric = CostMetric.from_result(result, default_model)
        with self._lock:
            self.metrics.append(metric)
        return metric

    def check_budget(self) -> tuple[bool, str | None]:
        """
        Checks whether the budget has been exceeded.

        Returns:
            (exceeded, reason) where reason is None if not exceeded
        """
        with self._lock:
            return self.budget.exceeded(self.total_tokens, self.total_cost)

    def check_loop(self, tool_sequence: list[str], args_normalizados: dict) -> bool:
        """
        Detects whether the loop threshold has been reached.

        Args:
            tool_sequence: Last N tool names (last 3)
            args_normalizados: Normalized invocation arguments

        Returns:
            True if a loop was detected (threshold reached)
        """
        import hashlib

        current_idx = len(self.metrics)

        # Compute current loop signature
        seq_str = ", ".join(tool_sequence[-3:]) if tool_sequence[-3:] else "empty"
        # canonical_json: nested dicts/lists serialize deterministically
        # (sorted keys) — f"{k}:{v}" gave different signatures for equal
        # args with different insertion order (false negatives).
        from ..core.crypto import canonical_json
        args_str = canonical_json(args_normalizados)

        loop_id = hashlib.sha256(f"{seq_str}|{args_str}".encode()).hexdigest()

        # Add to history
        with self._lock:
            self.loop_signatures.append(loop_id)

        # Check whether this loop repeated threshold times
        with self._lock:
            loops_recentes = [
                sig for sig in self.loop_signatures[-self.loop_threshold * 2 :]
                if sig == loop_id
            ]

        if len(loops_recentes) >= self.loop_threshold:
            suggestion = (
                "Reverify condition"
                if current_idx % 2 == 0
                else "Reinject knowledge" if "condition" in args_normalizados else "Close"
            )
            raise LoopViolationError(
                loop_signature=loop_id, suggested_action=suggestion
            )

        return False

    def record_budget_event(self, store):
        """
        Records a cost event in the store.

        Args:
            store: EventStore (InMemoryEventStore or store_pg)
        """
        from ..core.events import COST_EVENT, EventDraft

        budget_summary = {
            "execution_id": self.execution_id,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "total_tokens": self.total_tokens,
            "total_cost_usd": float(self.total_cost),
            "max_tokens_limit": self.budget.max_tokens,
            "max_usd_limit": float(self.budget.max_usd) if self.budget.max_usd else None,
            "exceeded": self.budget.max_tokens is not None and self.total_tokens >= self.budget.max_tokens,
            "exceeded_by_cost": self.budget.max_usd is not None and (Decimal(0) + self.total_cost) >= self.budget.max_usd,
        }

        draft = EventDraft(
            type=COST_EVENT,
            task_id=None,
            execution_id=self.execution_id,
            payload={"budget": budget_summary, "metrics": [
                {"tool": m.tool, "model": m.model, "tokens": m.tokens_used, "cost": float(m.cost_usd)}
                for m in self.metrics
            ]},
        )
        store.append(draft, stream=f"budget:{self.execution_id}")