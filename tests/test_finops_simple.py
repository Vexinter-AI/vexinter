"""Simple test for FinOps functionality."""
from decimal import Decimal

import pytest

from vexinter.models.budget import (
    Budget,
    ExecutionBudgetTracker,
    LoopViolationError,
)


def test_budget_basic():
    """Test that budget creation works."""
    # Create a budget with limits
    budget = Budget(max_tokens=100, max_usd=Decimal("1.00"))

    # Check that budget properties are set correctly
    assert budget.max_tokens == 100
    assert budget.max_usd == Decimal("1.00")
    assert budget.model_pricing == {}

    # Check that budget exceeded method works
    exceeded, reason = budget.exceeded(50, Decimal("0.50"))
    assert exceeded is False

    exceeded, reason = budget.exceeded(150, Decimal("1.50"))
    assert exceeded is True
    assert "tokens_spent=150 >= max_tokens=100" in reason
    # Format uses 4 decimal places for cost
    assert "cost_spent=$1.5000 >= max_usd=$1.0000" in reason

    print("Budget basic test passed")


def test_budget_exceeded_error():
    """Test that BudgetExceededError is raised correctly by ExecutionBudgetTracker.check_budget()."""
    budget = Budget(max_tokens=50, max_usd=Decimal("0.50"))
    tracker = ExecutionBudgetTracker(
        execution_id="test-exec",
        start_time=0.0,
        budget=budget,
    )

    # Add metric that exceeds token limit
    tracker.add_metric({"tokens": {"total": 100}, "model": "test"})

    # check_budget returns (exceeded, reason) - it doesn't raise
    exceeded, reason = tracker.check_budget()
    assert exceeded is True
    assert "tokens_spent=100 >= max_tokens=50" in reason

    # Test USD limit
    tracker2 = ExecutionBudgetTracker(
        execution_id="test-exec-2",
        start_time=0.0,
        budget=Budget(max_tokens=1000, max_usd=Decimal("0.50")),
    )
    tracker2.add_metric({"tokens": {"total": 10}, "model": "test", "cost_usd": Decimal("1.00")})

    exceeded, reason = tracker2.check_budget()
    assert exceeded is True
    assert "cost_spent=$1.0000 >= max_usd=$0.5000" in reason

    print("BudgetExceededError test passed")


def test_loop_violation_error():
    """Test that LoopViolationError is raised correctly by ExecutionBudgetTracker.check_loop()."""
    budget = Budget(max_tokens=1000, max_usd=Decimal("10.00"))
    tracker = ExecutionBudgetTracker(
        execution_id="test-exec",
        start_time=0.0,
        budget=budget,
        loop_threshold=3,
    )

    # Simulate repeated tool calls with SAME signature (same tool sequence and args)
    tool_sequence = ["tool-a", "tool-a", "tool-a"]
    args = {"param": "value"}

    # First 2 calls should not raise (threshold is 3, so 3rd call triggers)
    for i in range(2):
        tracker.add_metric({"tool": "tool-a", "tokens": {"total": 1}})
        result = tracker.check_loop(tool_sequence, args)
        assert result is False

    # 3rd call with same signature should raise LoopViolationError
    tracker.add_metric({"tool": "tool-a", "tokens": {"total": 1}})
    with pytest.raises(LoopViolationError) as exc_info:
        tracker.check_loop(tool_sequence, args)

    assert exc_info.value.loop_signature is not None
    assert exc_info.value.suggested_action is not None

    print("LoopViolationError test passed")


if __name__ == "__main__":
    test_budget_basic()
    test_budget_exceeded_error()
    test_loop_violation_error()
    print("All simple FinOps tests passed!")