"""Policy Engine (Fase 4) - authorization and compliance policy engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .events import POLICY_VIOLATION, EventDraft


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class PolicyRule:
    id: str
    name: str
    description: str
    effect: PolicyDecision
    conditions: dict  # Conditions to evaluate
    priority: int = 0


@dataclass
class PolicyContext:
    agent_id: str
    tool: str
    args: dict
    task_id: str
    execution_id: str
    instance_id: str | None = None
    credential_id: str | None = None
    metadata: dict = field(default_factory=dict)


class PolicyEngine:
    """Evaluates policies before invoking tools (§3)."""

    def __init__(self, store=None):
        self._store = store
        self._rules: list[PolicyRule] = []
        self._enabled = True

    def add_rule(self, rule: PolicyRule):
        self._rules.append(rule)
        self._rules.sort(key=lambda r: -r.priority)

    def remove_rule(self, rule_id: str) -> bool:
        for i, r in enumerate(self._rules):
            if r.id == rule_id:
                self._rules.pop(i)
                return True
        return False

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        if not self._enabled:
            return PolicyDecision.ALLOW

        for rule in self._rules:
            if self._match(rule, ctx):
                # Only non-allow decisions are violations worth an audit
                # event; allowed calls are already covered by TOOL_INVOKED.
                if rule.effect != PolicyDecision.ALLOW:
                    self._emit_violation(ctx, rule)
                return rule.effect

        return PolicyDecision.ALLOW

    _MAX_COND_DEPTH = 8

    def _match(self, rule: PolicyRule, ctx: PolicyContext) -> bool:
        return self._match_cond(rule.conditions or {}, ctx)

    def _match_cond(self, cond: dict, ctx: PolicyContext,
                    depth: int = 0) -> bool:
        """Recursive condition matcher.

        A node is either a combinator ({"all": [...]}, {"any": [...]},
        {"not": {...}}) or a leaf with the simple keys below; combinator
        nodes do not mix leaf keys.
        """
        if not cond:
            return True
        if depth > self._MAX_COND_DEPTH:
            return False
        if "all" in cond:
            return all(self._match_cond(c, ctx, depth + 1)
                       for c in cond["all"])
        if "any" in cond:
            return any(self._match_cond(c, ctx, depth + 1)
                       for c in cond["any"])
        if "not" in cond:
            return not self._match_cond(cond["not"], ctx, depth + 1)

        # Simple match by agent
        if "agent_id" in cond and cond["agent_id"] != ctx.agent_id:
            return False

        # Match by tool (wildcard support)
        if "tool" in cond:
            tool_pattern = cond["tool"]
            if not self._match_pattern(ctx.tool, tool_pattern):
                return False

        # Match by args (subset match)
        if "args" in cond:
            for k, v in cond["args"].items():
                if ctx.args.get(k) != v:
                    return False

        # Match by metadata
        if "metadata" in cond:
            for k, v in cond["metadata"].items():
                if ctx.metadata.get(k) != v:
                    return False

        return True

    def _match_pattern(self, text: str, pattern: str) -> bool:
        """Wildcard pattern matching where * matches any sequence.

        Iterative two-pointer glob (single-star backtracking): O(n*m)
        worst case, no regex — a pattern like *a*a*a*... cannot trigger
        catastrophic backtracking (ReDoS).
        """
        if "*" not in pattern:
            return text == pattern
        t = p = 0
        star_p = star_t = -1
        while t < len(text):
            if p < len(pattern) and pattern[p] == text[t]:
                t += 1
                p += 1
            elif p < len(pattern) and pattern[p] == "*":
                star_p, star_t = p, t
                p += 1
            elif star_p != -1:
                p = star_p + 1
                star_t += 1
                t = star_t
            else:
                return False
        while p < len(pattern) and pattern[p] == "*":
            p += 1
        return p == len(pattern)

    def _emit_violation(self, ctx: PolicyContext, rule: PolicyRule):
        if self._store is None:
            return
        self._store.append(EventDraft(
            type=POLICY_VIOLATION,
            task_id=ctx.task_id,
            execution_id=ctx.execution_id,
            agent_id=ctx.agent_id,
            payload={
                "rule_id": rule.id,
                "rule_name": rule.name,
                "effect": rule.effect.value,
                "tool": ctx.tool,
                "args": ctx.args,
            }
        ), stream=ctx.task_id or "policy")

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def list_rules(self) -> list[PolicyRule]:
        return list(self._rules)


# Default rules
DEFAULT_RULES = [
    PolicyRule(
        id="block-dangerous-tools",
        name="Block dangerous tools without approval",
        description="Irreversible write tools require approval",
        effect=PolicyDecision.REQUIRE_APPROVAL,
        conditions={"tool": "*.write", "metadata": {"operation_class": "effectful_irreversible"}},
        priority=10,
    ),
    PolicyRule(
        id="block-admin-tools",
        name="Block admin tools for non-admins",
        description="Only agents with the admin role can invoke *.admin_* tools",
        effect=PolicyDecision.DENY,
        conditions={"tool": "*.admin_*"},
        priority=20,
    ),
]