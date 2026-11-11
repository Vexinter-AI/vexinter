from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .crypto import canonical_json, hash_payload
from .tools import Observation, SourceRouter

Outcome = Literal["met", "not_met", "inconclusive"]


class ConditionError(Exception):
    """Malformed condition or disallowed operation (§4.2)."""

RESERVED_BINDINGS = {"input", "steps"}

def _require(node, *keys):
    for k in keys:
        if k not in node:
            raise ConditionError(f"'{node['op']}' requires the field '{k}'")


def _allow(node, permitted):
    extra = set(node) - set(permitted)
    if extra:
        raise ConditionError(f"disallowed fields in '{node['op']}': {sorted(extra)}")


def validate_condition(node) -> None:
    if not isinstance(node, dict) or "op" not in node:
        raise ConditionError("each node must be an object with 'op'")
    op = node["op"]
    if op in ("all", "any"):
        _require(node, "args")
        if not isinstance(node["args"], list) or not node["args"]:
            raise ConditionError(f"'{op}' requires a non-empty 'args' list")
        for sub in node["args"]:
            validate_condition(sub)
    elif op == "not":
        _require(node, "arg"); validate_condition(node["arg"])
    elif op == "exists":
        _require(node, "system", "query"); _allow(node, {"op", "system", "query", "as"})
        alias = node.get("as")
        if alias is not None and (not isinstance(alias, str)
                                  or alias in RESERVED_BINDINGS):
            raise ConditionError(
                "'as' must be a string and cannot be a reserved name "
                f"({sorted(RESERVED_BINDINGS)})")
    elif op == "count":
        _require(node, "system", "query", "equals")
        _allow(node, {"op", "system", "query", "equals"})
    elif op == "field_equals":
        _require(node, "from", "path", "equals")
        _allow(node, {"op", "from", "path", "equals"})
    elif op == "var_equals":
        _require(node, "a", "b"); _allow(node, {"op", "a", "b"})
    elif op == "within_window":
        _require(node, "seconds", "arg"); _allow(node, {"op", "seconds", "arg"})
        if not isinstance(node["seconds"], (int, float)) or node["seconds"] <= 0:
            raise ConditionError("'seconds' must be a positive number")
        validate_condition(node["arg"])
    elif op == "snapshot_changed":
        _require(node, "system", "query")
        _allow(node, {"op", "system", "query", "ignore"})
    else:
        raise ConditionError(f"disallowed operation: {op!r}")


def lookup_path(scope: dict, path: str):
    cur = scope
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConditionError(f"unresolved reference: '{path}'")
        cur = cur[part]
    return cur


_MAX_DEPTH = 100


def substitute_refs(obj, ctx: EvalContext, _depth: int = 0):
    """Resolves {"$ref": "client.id"} against the scope (input + bindings)."""
    if _depth > _MAX_DEPTH:
        raise ConditionError("reference nesting too deep")
    if isinstance(obj, dict):
        if set(obj) == {"$ref"}:
            return lookup_path(ctx.scope, obj["$ref"])
        return {k: substitute_refs(v, ctx, _depth + 1)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_refs(v, ctx, _depth + 1) for v in obj]
    return obj


def snapshot_key(system: str, query: dict) -> str:
    return f"{system}|{canonical_json(query)}"


class Evidence(BaseModel):
    system: str | None = None
    query: dict | None = None
    observed: Any = None
    observed_at: datetime | None = None
    consistency: str | None = None
    source: str | None = None
    note: str | None = None


class NodeResult(BaseModel):
    op: str
    outcome: Outcome
    provisional: bool = False       
    followup: bool = False         
    reason: str | None = None      
    detail: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    sensitive: bool = False        
    children: list[NodeResult] = Field(default_factory=list)

    def flatten(self) -> list[dict]:
        out = [{"op": self.op, "outcome": self.outcome,
                "detail": None if self.sensitive else self.detail,
                "provisional": self.provisional}]
        for c in self.children:
            out.extend(c.flatten())
        return out

    def evidence_summary(self, privacy_salt: str | None = None) -> list[dict]:
        """Metadata for each read for the record (§4.1): source,
        consistency and hash of what was observed. NEVER the content: the
        record is append-only and redaction (§16) does not exist yet.

        Allows injecting an optional privacy_salt to avoid dictionary attacks.
        """
        import secrets
        salt = privacy_salt or secrets.token_hex(16)
        out = []
        for ev in self.evidence:
            try:
                # Include salt in the hash for robust privacy (Item 8)
                h = hash_payload({"observed": ev.observed, "salt": salt})
            except Exception:  # noqa: BLE001 - error boundary: converted into result/log
                h = None
            out.append({"op": self.op, "system": ev.system,
                        "source": ev.source, "consistency": ev.consistency,
                        "observed_at": (ev.observed_at.isoformat()
                                        if ev.observed_at else None),
                        "observed_hash": h, "note": ev.note,
                        "privacy_salt_used": bool(salt)})
        for c in self.children:
            out.extend(c.evidence_summary(privacy_salt=salt))
        return out


NodeResult.model_rebuild()


class EvalContext:
    def __init__(self, task_input: dict, pre_snapshot: dict[str, Observation]):
        self.task_input = task_input
        self.pre_snapshot = pre_snapshot        
        self.bindings: dict[str, Any] = {}      
        self.binding_status: dict[str, str] = {}
        self.claimed: set[str] = {"steps"}

    @property
    def scope(self) -> dict:
        return {"input": self.task_input, **self.bindings}


def combine_all(outcomes: list[Outcome]) -> Outcome:
    if "not_met" in outcomes: return "not_met"
    if "inconclusive" in outcomes: return "inconclusive"
    return "met"


def combine_any(outcomes: list[Outcome]) -> Outcome:
    if "met" in outcomes: return "met"
    if "inconclusive" in outcomes: return "inconclusive"
    return "not_met"


_MISSING = object()


def eval_condition(node: dict, ctx: EvalContext, router: SourceRouter,
                   _depth: int = 0) -> NodeResult:
    if _depth > _MAX_DEPTH:
        raise ConditionError("condition nesting too deep")
    op = node["op"]

    if op in ("all", "any"):
        children = [eval_condition(sub, ctx, router, _depth + 1)
                    for sub in node["args"]]
        outcomes = [c.outcome for c in children]
        combined = combine_all(outcomes) if op == "all" else combine_any(outcomes)
        return NodeResult(op=op, outcome=combined,
                          provisional=any(c.provisional for c in children),
                          followup=any(c.followup for c in children),
                          children=children)

    if op == "not":
        child = eval_condition(node["arg"], ctx, router, _depth + 1)
        inv: dict[Outcome, Outcome] = {"met": "not_met", "not_met": "met",
               "inconclusive": "inconclusive"}
        return NodeResult(op=op, outcome=inv[child.outcome],
                          provisional=child.provisional, followup=child.followup,
                          children=[child])

    if op == "within_window":
        child = eval_condition(node["arg"], ctx, router, _depth + 1)
        if child.outcome == "inconclusive" and child.reason == "consistency":
            return NodeResult(op=op, outcome="met", provisional=True,
                              followup=True,
                              detail="provisional compliance within the window",
                              children=[child])
        return NodeResult(op=op, outcome=child.outcome,
                          provisional=child.provisional, followup=child.followup,
                          children=[child])

    if op == "exists":     return _leaf_exists(node, ctx, router)
    if op == "count":      return _leaf_count(node, ctx, router)
    if op == "field_equals": return _leaf_field_equals(node, ctx)
    if op == "var_equals": return _leaf_var_equals(node, ctx)
    if op == "snapshot_changed": return _leaf_snapshot_changed(node, ctx, router)
    raise ConditionError(f"unknown operation: {op}")


def _fetch(node, ctx, router, many=False):
    system = node["system"]
    try:
        query = substitute_refs(node["query"], ctx)
    except ConditionError as e:
        return None, Evidence(system=system, query={}, observed=None,
                              note=str(e))
        
    q = {**query, "many": True} if many else dict(query)
    # Identical sub-conditions re-evaluate the same external query: memoize
    # per EvalContext (one verification) so a duplicated node does not hit
    # the source twice.
    try:
        from .crypto import canonical_json as _cj
        memo = getattr(ctx, "_fetch_memo", None)
        if memo is None:
            memo = ctx._fetch_memo = {}
        mkey = _cj({"system": system, "q": q})
    except Exception:  # noqa: BLE001 - unserializable query: skip memo
        memo, mkey = None, None
    if memo is not None and mkey in memo:
        return memo[mkey]
    try:
        obs = router.observe(system, q)
    except Exception as e:      # noqa: BLE001 - error boundary: converted into result/log
        out = (None, Evidence(system=system, query=query,
                              note=f"source unavailable: {e}"))
        if memo is not None:
            memo[mkey] = out
        return out
    ev = Evidence(system=system, query=query, observed=obs.data,
                  observed_at=obs.observed_at, consistency=obs.consistency,
                  source=obs.source)
    out = (obs, ev)
    if memo is not None:
        memo[mkey] = out
    return out


def _leaf_exists(node, ctx, router):
    obs, ev = _fetch(node, ctx, router)
    if obs is None:
        if "as" in node: ctx.binding_status[node["as"]] = "inconclusive"
        sin_ref = (ev.note or "").startswith("unresolved reference")
        return NodeResult(op="exists", outcome="inconclusive",
                          reason="unresolved_reference" if sin_ref else "unavailable",
                          detail=(ev.note if sin_ref
                                  else "the source could not be queried"),
                          evidence=[ev], sensitive=True)
    if obs.consistency != "strong":
        if "as" in node: ctx.binding_status[node["as"]] = "inconclusive"
        return NodeResult(op="exists", outcome="inconclusive",
                          reason="consistency",
                          detail=f"read with consistency '{obs.consistency}'",
                          evidence=[ev])
    data = obs.data
    if isinstance(data, list):
        vals = [x for x in data if x is not None]
        found = bool(vals)
        bound = vals[0] if vals else None
    else:
        found = data is not None
        bound = data
    if "as" in node:
        if found:
            ctx.bindings[node["as"]] = bound
            ctx.binding_status[node["as"]] = "met"
        elif isinstance(data, list) and data and bound is None:
            # Adapter returned rows but every value was None: unknown, not absent.
            ctx.binding_status[node["as"]] = "inconclusive"
        else:
            ctx.binding_status[node["as"]] = "not_met"
    return NodeResult(op="exists", outcome="met" if found else "not_met",
                      evidence=[ev])


def _leaf_count(node, ctx, router):
    obs, ev = _fetch(node, ctx, router, many=True)
    if obs is None:
        sin_ref = (ev.note or "").startswith("unresolved reference")
        return NodeResult(op="count", outcome="inconclusive",
                          reason="unresolved_reference" if sin_ref else "unavailable",
                          detail=ev.note, evidence=[ev], sensitive=True)
    if obs.consistency != "strong":
        return NodeResult(op="count", outcome="inconclusive",
                          reason="consistency", evidence=[ev])
    n = len(obs.data) if isinstance(obs.data, list) else (0 if obs.data is None else 1)
    expected = node["equals"]
    return NodeResult(op="count", outcome="met" if n == expected else "not_met",
                      detail=f"count={n}, expected={expected}", evidence=[ev])


def _binding_value(ctx, name, path, default=_MISSING):
    if name not in ctx.bindings:
        return default
    cur = ctx.bindings[name]
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _leaf_field_equals(node, ctx):
    name, path = node["from"], node["path"]
    if name in ctx.claimed:
        return _not_independent("field_equals", name)
    actual = _binding_value(ctx, name, path)
    if actual is _MISSING:
        if ctx.binding_status.get(name) == "inconclusive":
            return NodeResult(op="field_equals", outcome="inconclusive",
                              reason="consistency",
                              detail=f"'{name}' could not be observed conclusively")
        return NodeResult(op="field_equals", outcome="not_met",
                          detail=f"missing field: {name}.{path}")
    expected = node["equals"]
    if isinstance(expected, dict) and set(expected) == {"$ref"}:
        try:
            expected = lookup_path(ctx.scope, expected["$ref"])
        except ConditionError as e:
            return NodeResult(op="field_equals", outcome="inconclusive",
                              reason="unresolved_reference", detail=str(e),
                              sensitive=True)
    return NodeResult(op="field_equals",
                      outcome="met" if actual == expected else "not_met",
                      detail=f"{name}.{path}={actual!r} vs {expected!r}",
                      sensitive=True)


def _not_independent(op: str, name: str) -> NodeResult:
    """A value reported by a tool proves nothing about itself
    (§4.1): it can never yield 'met' or 'not_met'."""
    return NodeResult(
        op=op, outcome="inconclusive", reason="not_independent",
        detail=f"'{name}' comes from what a tool or the agent reported, "
               "not from an independent observation")


def _leaf_var_equals(node, ctx):
    """Relation between two linked records (§7.1: invoice ↔ customer)."""
    for lado in ("a", "b"):
        if node[lado]["from"] in ctx.claimed:
            return _not_independent("var_equals", node[lado]["from"])
    a = _binding_value(ctx, node["a"]["from"], node["a"]["path"])
    b = _binding_value(ctx, node["b"]["from"], node["b"]["path"])
    if a is _MISSING or b is _MISSING:
        st = [ctx.binding_status.get(node["a"]["from"]),
              ctx.binding_status.get(node["b"]["from"])]
        if "inconclusive" in st:
            return NodeResult(op="var_equals", outcome="inconclusive",
                              reason="consistency",
                              detail="record not observable conclusively")
        return NodeResult(op="var_equals", outcome="not_met", detail="record missing")
    return NodeResult(op="var_equals", outcome="met" if a == b else "not_met",
                      detail=f"{a!r} vs {b!r}", sensitive=True)


def _leaf_snapshot_changed(node, ctx, router):
    """Comparison against the initial snapshot (§4.5)."""
    system = node["system"]
    try:
        query = substitute_refs(node["query"], ctx)
    except ConditionError as e:
        return NodeResult(op="snapshot_changed", outcome="inconclusive",
                          reason="unresolved_reference", detail=str(e))
    pre = ctx.pre_snapshot.get(snapshot_key(system, query))
    if pre is None:
        return NodeResult(op="snapshot_changed", outcome="inconclusive",
                          detail="no previous snapshot for this query")
    try:
        obs = router.observe(system, dict(query))
    except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
        return NodeResult(op="snapshot_changed", outcome="inconclusive",
                          reason="unavailable", detail=str(e), sensitive=True)
    if obs.consistency != "strong":
        return NodeResult(op="snapshot_changed", outcome="inconclusive",
                          reason="consistency",
                          detail=f"consistency '{obs.consistency}'")
    ignore = set(node.get("ignore", []))
    changed = not _same(pre.data, obs.data, ignore)
    return NodeResult(op="snapshot_changed",
                      outcome="met" if changed else "not_met",
                      detail=f"change relative to initial snapshot: {changed}",
                      evidence=[Evidence(system=system, query=query,
                                         observed=obs.data,
                                         observed_at=obs.observed_at,
                                         consistency=obs.consistency,
                                         source=obs.source)])


def _same(a, b, ignore=frozenset()):
    if isinstance(a, dict) and isinstance(b, dict):
        ka = {k for k in a if k not in ignore}
        kb = {k for k in b if k not in ignore}
        return ka == kb and all(_same(a[k], b[k], ignore) for k in ka)
    return a == b