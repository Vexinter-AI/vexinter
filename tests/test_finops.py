"""Test FinOps (budget) functionality."""
from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from vexinter.core.store import InMemoryEventStore
from vexinter.core.taskrun import TaskRun
from vexinter.core.tools import (
    CallContext,
    Intermediary,
    Observation,
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
        from vexinter.core.tools import Observation
        return Observation(data=None, observed_at=datetime.now(), source="mock", consistency="strong")


def test_budget_enforcement():
    """Test that budget enforcement works correctly."""
    # Create a budget with limits (this is the task's budget)
    task_budget = Budget(max_tokens=100, max_usd=Decimal("1.00"))

    # Create a task run with this budget
    task = TaskRun(
        task_id="test-task",
        execution_id="test-exec",
        agent_id="test-agent",
        objective="test objective",
        condition={},
        budget=task_budget,
    )

    # Create a mock observation that consumes tokens
    class MockObservation(Observation):
        def __init__(self):
            super().__init__(
                data={
                    "tokens": {"total": 50},  # 50 tokens consumed
                    "model": "test-model",
                },
                observed_at=datetime.now(),
                source="test-source",
                consistency="strong",
            )

    # Add observations to the task's pre_snapshot
    task.pre_snapshot = {
        "test-source": MockObservation()
    }

    # Create event store
    event_store = InMemoryEventStore()

    def mock_invoke(tool, req):
        return OperationResult(ok=True, data={"tokens": {"total": 60}, "model": "test-model"}, error=None)

    adapter_mock = MockAdapter(mock_invoke)
    authorizer = StaticAuthorizer()
    authorizer.can_invoke = Mock(return_value=True)

    # Use task_budget as default_budget for the intermediary
    intermediary = Intermediary(
        store=event_store,
        authorizer=authorizer,
        adapters=[adapter_mock],
        default_budget=task_budget,
    )

    # Create a context
    ctx = CallContext(
        agent_id="test-agent",
        task_id="test-task",
        execution_id="test-exec",
        stream="task:test-task",
        instance_id=None,
        credential_id=None,
    )

    # First call: 60 tokens
    intermediary.call(ctx, "test-system.test-tool", {"input": "test"}, {})

    # Second call: 60 tokens (total 120 > 100 limit)
    with pytest.raises(BudgetExceededError):
        intermediary.call(ctx, "test-system.test-tool", {"input": "test2"}, {})

    print("Budget enforcement test completed")


def test_loop_detection():
    """Test that loop detection works correctly."""
    # Create a budget with no limits (so budget check passes)
    budget = Budget(max_tokens=1000, max_usd=Decimal("10.00"))

    # Create a task run with this budget
    task = TaskRun(
        task_id="loop-test",
        execution_id="loop-exec",
        agent_id="test-agent",
        objective="loop test",
        condition={},
        budget=budget,
    )

    # Create event store
    event_store = InMemoryEventStore()

    def mock_invoke(tool, req):
        return OperationResult(ok=True, data={"tokens": {"total": 1}, "model": "test-model"}, error=None)

    adapter_mock = MockAdapter(mock_invoke)
    authorizer = StaticAuthorizer()
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

    # Add some metrics to simulate loop
    task.pre_snapshot = {}

    # Call intermediary multiple times with SAME args to simulate loop (threshold=3)
    same_args = {"input": "same"}
    for i in range(4):
        intermediary.call(ctx, "test-system.test-tool", same_args, {})

    # 5th call with same args should raise LoopViolationError
    with pytest.raises(LoopViolationError):
        intermediary.call(ctx, "test-system.test-tool", same_args, {})

    print("Loop detection test completed")


if __name__ == "__main__":
    test_budget_enforcement()
    test_loop_detection()
    print("All FinOps tests passed!")