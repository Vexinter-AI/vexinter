"""Test to verify budget and loop detection functionality is working correctly."""
from decimal import Decimal
from unittest.mock import Mock

import pytest

from vexinter.core.events import EventType
from vexinter.core.store import InMemoryEventStore
from vexinter.core.tools import (
    CallContext,
    Intermediary,
    OperationClass,
    OperationResult,
    StaticAuthorizer,
    ToolSpec,
)
from vexinter.models.budget import Budget, BudgetExceededError, LoopViolationError


class MockAdapter:
    """Mock adapter that provides a test tool."""
    system = "test-system"
    
    def __init__(self, invoke_func):
        self._invoke_func = invoke_func
        self._tools = {
            "test-tool": ToolSpec(
                system="test-system",
                name="test-tool",
                description="Test tool",
                operation_class=OperationClass.READ,
                supports_idempotency=False,
                effect_probe=None,
            )
        }
    
    def tools(self) -> dict:
        return self._tools
    
    def invoke(self, tool: str, req):
        return self._invoke_func(tool, req)
    
    def observe(self, query: dict):
        from datetime import datetime

        from vexinter.core.tools import Observation
        return Observation(data=None, observed_at=datetime.now(), source="mock", consistency="strong")


def test_budget_enforcement_workflow():
    """Test that budget enforcement works in the complete workflow."""
    # Task budget: 50 tokens limit
    task_budget = Budget(max_tokens=50, max_usd=Decimal("0.50"))
    event_store = InMemoryEventStore()

    def mock_invoke(tool, req):
        return OperationResult(ok=True, data={"tokens": {"total": 30}, "model": "test-model"}, error=None)

    adapter_mock = MockAdapter(mock_invoke)
    authorizer = Mock()
    authorizer.can_invoke = Mock(return_value=True)

    # Use task_budget as default_budget for the intermediary
    intermediary = Intermediary(
        store=event_store,
        authorizer=authorizer,
        adapters=[adapter_mock],
        default_budget=task_budget,
    )

    ctx = CallContext(
        agent_id="test-agent",
        task_id="test-task",
        execution_id="test-exec",
        stream="task:test-task",
        instance_id=None,
        credential_id=None,
    )

    # First call: 30 tokens
    intermediary.call(ctx, "test-system.test-tool", {"input": "test1"}, {})

    # Second call: 30 tokens (total 60 > 50 limit)
    with pytest.raises(BudgetExceededError):
        intermediary.call(ctx, "test-system.test-tool", {"input": "test2"}, {})

    events = event_store.query()
    cost_events = [e for e in events if e.type == EventType.COST_EVENT]
    assert len(cost_events) == 1
    print("Budget enforcement test passed!")


def test_loop_detection_workflow():
    """Test that loop detection works correctly."""
    budget = Budget(max_tokens=1000, max_usd=Decimal("10.00"))
    event_store = InMemoryEventStore()

    def mock_invoke(tool, req):
        return OperationResult(ok=True, data={"tokens": {"total": 1}, "model": "test-model"}, error=None)

    adapter_mock = MockAdapter(mock_invoke)
    authorizer = Mock()
    authorizer.can_invoke = Mock(return_value=True)

    intermediary = Intermediary(
        store=event_store,
        authorizer=authorizer,
        adapters=[adapter_mock],
        default_budget=budget,
    )

    ctx = CallContext(
        agent_id="test-agent",
        task_id="loop-test",
        execution_id="loop-exec",
        stream="task:loop-test",
        instance_id=None,
        credential_id=None,
    )

    # Call intermediary 4 times with SAME args to simulate loop (threshold=3)
    same_args = {"input": "same"}
    for i in range(4):
        intermediary.call(ctx, "test-system.test-tool", same_args, {})

    # 5th call with same args should raise LoopViolationError
    with pytest.raises(LoopViolationError):
        intermediary.call(ctx, "test-system.test-tool", same_args, {})

    events = event_store.query()
    loop_events = [e for e in events if e.type == EventType.LOOP_DETECTED]
    assert len(loop_events) == 1
    print("Loop detection test passed!")


if __name__ == "__main__":
    test_budget_enforcement_workflow()
    test_loop_detection_workflow()
    print("All verification tests passed!")

def test_loop_threshold_is_configurable_per_budget():
    """Budget.loop_threshold overrides the service default per execution."""
    from vexinter.models.budget import Budget
    intermediary = Intermediary(
        InMemoryEventStore(), StaticAuthorizer(), [],
        default_budget=Budget())
    t = intermediary._get_or_create_tracker("exec_x", Budget(loop_threshold=9))
    assert t.loop_threshold == 9
    t2 = intermediary._get_or_create_tracker("exec_y")
    assert t2.loop_threshold == 3
