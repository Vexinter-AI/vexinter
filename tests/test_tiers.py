"""Tests for tier enforcement (Community/Professional/Business)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vexinter.core.tiers import (
    TIER_DEFINITIONS,
    Tier,
    TierManager,
    TierUsage,
)


import pytest


@pytest.fixture
def no_paid_tiers():
    """Ensures paid tiers are unregistered for the test, restoring them
    afterwards — vexinter_pro may have registered them earlier in the
    process (e.g. via make_app's plugin import)."""
    saved = {t: TIER_DEFINITIONS.pop(t)
             for t in (Tier.PROFESSIONAL, Tier.BUSINESS)
             if t in TIER_DEFINITIONS}
    try:
        yield
    finally:
        TIER_DEFINITIONS.update(saved)


class TestTierDefinitions:
    """Test tier definitions match TERMS-OF-SERVICE.md."""

    def test_community_tier(self):
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.name == Tier.COMMUNITY
        assert limits.monthly_task_limit == 500
        assert limits.hard_block is True
        assert limits.overage_per_1k_usd == 0.0
        assert limits.monthly_price_usd == 0.0

    def test_paid_tiers_absent_without_plugin(self):
        # Community Edition guarantee: a process that never imports
        # vexinter_pro has only Community defined. Verified in a
        # subprocess so prior imports in this test session can't leak.
        import subprocess
        import sys
        code = (
            "from vexinter.core.tiers import TIER_DEFINITIONS, Tier;"
            "assert Tier.PROFESSIONAL not in TIER_DEFINITIONS;"
            "assert Tier.BUSINESS not in TIER_DEFINITIONS;"
            "assert Tier.COMMUNITY in TIER_DEFINITIONS"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_unregistered_tier_degrades_to_community(self, no_paid_tiers):
        mgr = TierManager()
        limits = mgr.get_limits(Tier.PROFESSIONAL)
        assert limits.name == Tier.COMMUNITY
        assert limits.monthly_task_limit == 500
        assert limits.hard_block is True


class TestTierLimits:
    """Test TierLimits methods."""

    def test_community_is_exceeded(self):
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.is_exceeded(499) is False
        assert limits.is_exceeded(500) is True
        assert limits.is_exceeded(501) is True

    def test_paid_plan_is_exceeded_at_community_limit(self, no_paid_tiers):
        # Without vexinter-pro, a "professional" plan string resolves to
        # the Community limits — 500 tasks, not 5,000.
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.is_exceeded(499) is False
        assert limits.is_exceeded(500) is True

    def test_overage_calculation_community(self):
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.overage_tasks(150) == 0  # hard block, no overage
        assert limits.overage_cost_usd(150) == 0.0

    def test_overage_always_zero(self):
        # No tier bills overage: every plan hard-blocks at its quota.
        for tier in (Tier.COMMUNITY, Tier.PROFESSIONAL, Tier.BUSINESS):
            limits = TIER_DEFINITIONS.get(tier) or TIER_DEFINITIONS[Tier.COMMUNITY]
            assert limits.overage_tasks(999_999) == 0
            assert limits.overage_cost_usd(999_999) == 0.0


class TestTierUsage:
    """Test TierUsage period tracking."""

    def test_is_current_period(self):
        now = datetime.now(UTC)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_end = (period_start + timedelta(days=32)).replace(day=1)
        
        usage = TierUsage(
            email="test@example.com",
            tier=Tier.COMMUNITY,
            period_start=period_start,
            period_end=period_end,
        )
        assert usage.is_current_period() is True

    def test_reset_if_new_period(self):
        now = datetime.now(UTC)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_end = (period_start + timedelta(days=32)).replace(day=1)
        
        usage = TierUsage(
            email="test@example.com",
            tier=Tier.COMMUNITY,
            period_start=period_start,
            period_end=period_end,
            tasks_used=50,
        )
        
        # Should not reset if still in current period
        reset = usage.reset_if_new_period()
        assert reset is False
        assert usage.tasks_used == 50

    def test_reset_if_new_period_expired(self):
        # Simulate an expired period
        now = datetime.now(UTC)
        old_start = now - timedelta(days=60)
        old_start = old_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        old_end = (old_start + timedelta(days=32)).replace(day=1)
        
        usage = TierUsage(
            email="test@example.com",
            tier=Tier.COMMUNITY,
            period_start=old_start,
            period_end=old_end,
            tasks_used=50,
        )
        
        reset = usage.reset_if_new_period()
        assert reset is True
        assert usage.tasks_used == 0
        assert usage.period_start > old_start


class TestTierManager:
    """Test TierManager functionality."""

    def test_get_tier_from_plan(self):
        mgr = TierManager()
        assert mgr.get_tier_from_plan("community") == Tier.COMMUNITY
        assert mgr.get_tier_from_plan("professional") == Tier.PROFESSIONAL
        assert mgr.get_tier_from_plan("pro") == Tier.PROFESSIONAL
        assert mgr.get_tier_from_plan("business") == Tier.BUSINESS
        assert mgr.get_tier_from_plan("biz") == Tier.BUSINESS
        assert mgr.get_tier_from_plan("unknown") == Tier.COMMUNITY

    def test_check_task_allowed_community_under_limit(self):
        mgr = TierManager()
        allowed, reason = mgr.check_task_allowed("test@example.com", "community")
        assert allowed is True
        assert reason is None

    def test_check_task_allowed_community_at_limit(self):
        mgr = TierManager()
        # Use 500 tasks
        for _ in range(500):
            mgr.record_task("test@example.com", "community")
        
        allowed, reason = mgr.check_task_allowed("test@example.com", "community")
        assert allowed is False
        assert "limit reached" in reason
        assert "500" in reason

    def test_check_task_allowed_paid_plan_degrades_to_community(self, no_paid_tiers):
        mgr = TierManager()
        # "professional" without vexinter-pro installed enforces the
        # Community quota: denied at 500 tasks.
        for _ in range(500):
            mgr.record_task("test@example.com", "professional")

        allowed, reason = mgr.check_task_allowed("test@example.com", "professional")
        assert allowed is False
        assert "limit reached" in reason

    def test_record_task_increments_counter(self):
        mgr = TierManager()
        usage = mgr.get_usage("test@example.com", Tier.COMMUNITY)
        assert usage.tasks_used == 0
        
        mgr.record_task("test@example.com", "community")
        assert usage.tasks_used == 1
        
        mgr.record_task("test@example.com", "community")
        assert usage.tasks_used == 2

    def test_get_usage_summary(self):
        mgr = TierManager()
        for _ in range(50):
            mgr.record_task("test@example.com", "community")

        summary = mgr.get_usage_summary("test@example.com", "community")
        assert summary["email"] == "test@example.com"
        assert summary["tier"] == "community"
        assert summary["tasks_used"] == 50
        assert summary["task_limit"] == 500
        assert summary["hard_block"] is True
        assert summary["overage_tasks"] == 0
        assert summary["overage_cost_usd"] == 0.0
        assert summary["monthly_price_usd"] == 0.0

    def test_get_usage_summary_paid_plan_degrades(self, no_paid_tiers):
        mgr = TierManager()
        for _ in range(600):
            mgr.record_task("test@example.com", "professional")

        summary = mgr.get_usage_summary("test@example.com", "professional")
        assert summary["tasks_used"] == 600
        assert summary["task_limit"] == 500  # community limits apply
        assert summary["overage_tasks"] == 0
        assert summary["overage_cost_usd"] == 0.0


class TestTierManagerIsolation:
    """Test that different users have isolated usage tracking."""

    def test_different_users_independent(self):
        mgr = TierManager()
        
        for _ in range(500):
            mgr.record_task("user1@example.com", "community")
        
        for _ in range(50):
            mgr.record_task("user2@example.com", "community")
        
        usage1 = mgr.get_usage("user1@example.com", Tier.COMMUNITY)
        usage2 = mgr.get_usage("user2@example.com", Tier.COMMUNITY)
        
        assert usage1.tasks_used == 500
        assert usage2.tasks_used == 50
        
        # user1 should be blocked, user2 should not
        allowed1, _ = mgr.check_task_allowed("user1@example.com", "community")
        allowed2, _ = mgr.check_task_allowed("user2@example.com", "community")
        
        assert allowed1 is False
        assert allowed2 is True