"""Tests for tier enforcement (Community/Professional/Business)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vexinter.core.tiers import (
    TIER_DEFINITIONS,
    Tier,
    TierManager,
    TierUsage,
)


class TestTierDefinitions:
    """Test tier definitions match TERMS-OF-SERVICE.md."""

    def test_community_tier(self):
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.name == Tier.COMMUNITY
        assert limits.monthly_task_limit == 100
        assert limits.hard_block is True
        assert limits.overage_per_1k_usd == 0.0
        assert limits.monthly_price_usd == 0.0

    def test_professional_tier(self):
        limits = TIER_DEFINITIONS[Tier.PROFESSIONAL]
        assert limits.name == Tier.PROFESSIONAL
        assert limits.monthly_task_limit == 5_000
        assert limits.hard_block is False
        assert limits.overage_per_1k_usd == 5.0
        assert limits.monthly_price_usd == 79.0

    def test_business_tier(self):
        limits = TIER_DEFINITIONS[Tier.BUSINESS]
        assert limits.name == Tier.BUSINESS
        assert limits.monthly_task_limit == 50_000
        assert limits.hard_block is False
        assert limits.overage_per_1k_usd == 3.0
        assert limits.monthly_price_usd == 299.0


class TestTierLimits:
    """Test TierLimits methods."""

    def test_community_is_exceeded(self):
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.is_exceeded(99) is False
        assert limits.is_exceeded(100) is True
        assert limits.is_exceeded(101) is True

    def test_professional_is_exceeded(self):
        limits = TIER_DEFINITIONS[Tier.PROFESSIONAL]
        assert limits.is_exceeded(4_999) is False
        assert limits.is_exceeded(5_000) is True
        assert limits.is_exceeded(5_001) is True

    def test_overage_calculation_community(self):
        limits = TIER_DEFINITIONS[Tier.COMMUNITY]
        assert limits.overage_tasks(150) == 0  # hard block, no overage
        assert limits.overage_cost_usd(150) == 0.0

    def test_overage_calculation_professional(self):
        limits = TIER_DEFINITIONS[Tier.PROFESSIONAL]
        assert limits.overage_tasks(5_000) == 0
        assert limits.overage_tasks(5_500) == 500
        assert limits.overage_tasks(6_000) == 1_000
        assert limits.overage_cost_usd(5_500) == 2.5  # 500/1000 * $5
        assert limits.overage_cost_usd(6_000) == 5.0  # 1000/1000 * $5

    def test_overage_calculation_business(self):
        limits = TIER_DEFINITIONS[Tier.BUSINESS]
        assert limits.overage_tasks(50_000) == 0
        assert limits.overage_tasks(51_000) == 1_000
        assert limits.overage_cost_usd(51_000) == 3.0  # 1000/1000 * $3


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
        # Use 100 tasks
        for _ in range(100):
            mgr.record_task("test@example.com", "community")
        
        allowed, reason = mgr.check_task_allowed("test@example.com", "community")
        assert allowed is False
        assert "limit reached" in reason
        assert "100" in reason

    def test_check_task_allowed_professional_soft_block(self):
        mgr = TierManager()
        # Use 5000 tasks (at limit)
        for _ in range(5_000):
            mgr.record_task("test@example.com", "professional")
        
        # Should still be allowed (soft block)
        allowed, reason = mgr.check_task_allowed("test@example.com", "professional")
        assert allowed is True
        assert reason is None

    def test_check_task_allowed_business_soft_block(self):
        mgr = TierManager()
        # Use 50000 tasks (at limit)
        for _ in range(50_000):
            mgr.record_task("test@example.com", "business")
        
        # Should still be allowed (soft block)
        allowed, reason = mgr.check_task_allowed("test@example.com", "business")
        assert allowed is True
        assert reason is None

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
            mgr.record_task("test@example.com", "professional")
        
        summary = mgr.get_usage_summary("test@example.com", "professional")
        assert summary["email"] == "test@example.com"
        assert summary["tier"] == "professional"
        assert summary["tasks_used"] == 50
        assert summary["task_limit"] == 5_000
        assert summary["hard_block"] is False
        assert summary["overage_tasks"] == 0
        assert summary["overage_cost_usd"] == 0.0
        assert summary["monthly_price_usd"] == 79.0

    def test_get_usage_summary_with_overage(self):
        mgr = TierManager()
        for _ in range(6_000):
            mgr.record_task("test@example.com", "professional")
        
        summary = mgr.get_usage_summary("test@example.com", "professional")
        assert summary["tasks_used"] == 6_000
        assert summary["overage_tasks"] == 1_000
        assert summary["overage_cost_usd"] == 5.0


class TestTierManagerIsolation:
    """Test that different users have isolated usage tracking."""

    def test_different_users_independent(self):
        mgr = TierManager()
        
        for _ in range(100):
            mgr.record_task("user1@example.com", "community")
        
        for _ in range(50):
            mgr.record_task("user2@example.com", "community")
        
        usage1 = mgr.get_usage("user1@example.com", Tier.COMMUNITY)
        usage2 = mgr.get_usage("user2@example.com", Tier.COMMUNITY)
        
        assert usage1.tasks_used == 100
        assert usage2.tasks_used == 50
        
        # user1 should be blocked, user2 should not
        allowed1, _ = mgr.check_task_allowed("user1@example.com", "community")
        allowed2, _ = mgr.check_task_allowed("user2@example.com", "community")
        
        assert allowed1 is False
        assert allowed2 is True