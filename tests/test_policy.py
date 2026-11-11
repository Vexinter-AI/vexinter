"""Test Policy Engine functionality (Fase 4)."""
from vexinter import Vexinter
from vexinter.core.policy import PolicyContext, PolicyDecision, PolicyEngine, PolicyRule
from vexinter.core.store import InMemoryEventStore
from vexinter.core.tools import (
    AuthorizationError,
    Observation,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolAdapter,
    ToolSpec,
)


class MockAdapter(ToolAdapter):
    def __init__(self, system: str):
        self.system = system

    def tools(self) -> dict[str, ToolSpec]:
        return {
            "read": ToolSpec(system=self.system, name="read", description="Read data", input_schema={}, operation_class=OperationClass.READ),
            "write": ToolSpec(system=self.system, name="write", description="Write data", input_schema={}, operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE),
            "admin_op": ToolSpec(system=self.system, name="admin_op", description="Admin tool", input_schema={}, operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE),
        }

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        return OperationResult(ok=True, data={"result": "ok"})

    def observe(self, query: dict) -> Observation:
        return Observation(data=None, observed_at=None, source=self.system, consistency="strong")


def test_policy_engine_basic():
    engine = PolicyEngine()
    
    # Add custom rule
    engine.add_rule(PolicyRule(
        id="block-foo",
        name="Block foo tool",
        description="Block foo",
        effect=PolicyDecision.DENY,
        conditions={"tool": "sys.foo"},
        priority=10
    ))
    
    ctx = PolicyContext(
        agent_id="agent1",
        tool="sys.foo",
        args={},
        task_id="t1",
        execution_id="e1"
    )
    
    assert engine.evaluate(ctx) == PolicyDecision.DENY
    
    ctx2 = PolicyContext(
        agent_id="agent1",
        tool="sys.bar",
        args={},
        task_id="t1",
        execution_id="e1"
    )
    assert engine.evaluate(ctx2) == PolicyDecision.ALLOW
    print("PolicyEngine basic test passed!")


def test_vexinter_policy_integration():
    adapter = MockAdapter("sys")
    vex = Vexinter(embedded=True, adapters=[adapter])
    
    with vex.task(agent="agent1", objective="test policy", condition={"op": "exists", "system": "sys", "query": {}}, input={}) as t:
        # Default rule 'block-admin-tools' denies admin.* for non-admin
        try:
            t._rt.intermediary.call(t._run, "sys.admin_op", {})
            assert False, "Should have raised AuthorizationError"
        except AuthorizationError:
            pass
        
        # Test adding a custom policy rule via SDK (with high priority to override default)
        t.policy_add_rule(
            rule_id="custom-deny",
            name="Custom Deny",
            description="Deny write",
            effect="deny",
            conditions={"tool": "sys.write"},
            priority=50  # Higher priority than default rules
        )
        
        rules = t.policy_list_rules()
        assert any(r["id"] == "custom-deny" for r in rules)
        
        try:
            t._rt.intermediary.call(t._run, "sys.write", {})
            assert False, "Should have raised AuthorizationError due to custom policy"
        except AuthorizationError:
            pass
            
    print("Vexinter policy integration test passed!")


if __name__ == "__main__":
    test_policy_engine_basic()
    test_vexinter_policy_integration()
    print("All Policy Engine tests passed successfully!")


def _rule(rid, conditions, effect=PolicyDecision.DENY):
    return PolicyRule(id=rid, name=rid, description="t",
                      effect=effect, conditions=conditions)


def test_policy_conditions_support_combinators():
    """all/any/not sobre los matchers simples."""
    eng = PolicyEngine(InMemoryEventStore())
    ctx = PolicyContext(agent_id="a", tool="crm.admin_delete",
                        args={}, task_id="t", execution_id="e",
                        metadata={"operation_class": "write"})
    eng.add_rule(_rule("r1", {"not": {"metadata": {"operation_class": "read"}}}))
    assert eng.evaluate(ctx) == PolicyDecision.DENY
    ctx2 = PolicyContext(agent_id="a", tool="crm.admin_delete",
                         args={}, task_id="t", execution_id="e",
                         metadata={"operation_class": "read"})
    # el not lo excluye: no match -> allow
    assert eng.evaluate(ctx2) == PolicyDecision.ALLOW
    eng2 = PolicyEngine(InMemoryEventStore())
    eng2.add_rule(_rule("r2", {"all": [
        {"agent_id": "a"}, {"not": {"args": {"dry": True}}}]}))
    assert eng2.evaluate(ctx) == PolicyDecision.DENY
