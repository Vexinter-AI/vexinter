"""Tier management for Vexinter commercial plans.

Implements enforcement of Community/Professional/Business plan limits
as defined in TERMS-OF-SERVICE.md:

- Community ($0): hard block at 100 tasks/month
- Professional ($79/mo annual): soft block at 5,000 tasks/month + $5/1k overage
- Business ($299/mo annual): soft block at 50,000 tasks/month + $3/1k overage
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from vexinter.core.observability import get_correlation_id, log_audit_event


class Tier(str, Enum):
    """Subscription tiers."""
    COMMUNITY = "community"
    PROFESSIONAL = "professional"
    BUSINESS = "business"


@dataclass(frozen=True)
class TierLimits:
    """Limits for a specific tier."""
    name: Tier
    monthly_task_limit: int
    hard_block: bool  # True = hard block, False = soft block with overage
    overage_per_1k_usd: float  # 0 if hard_block=True
    monthly_price_usd: float

    def is_exceeded(self, tasks_used: int) -> bool:
        return tasks_used >= self.monthly_task_limit

    def overage_tasks(self, tasks_used: int) -> int:
        if self.hard_block:
            return 0
        return max(0, tasks_used - self.monthly_task_limit)

    def overage_cost_usd(self, tasks_used: int) -> float:
        if self.hard_block:
            return 0.0
        overage = self.overage_tasks(tasks_used)
        return (overage / 1000.0) * self.overage_per_1k_usd


# Tier definitions per TERMS-OF-SERVICE.md
TIER_DEFINITIONS: dict[Tier, TierLimits] = {
    Tier.COMMUNITY: TierLimits(
        name=Tier.COMMUNITY,
        monthly_task_limit=100,
        hard_block=True,
        overage_per_1k_usd=0.0,
        monthly_price_usd=0.0,
    ),
    Tier.PROFESSIONAL: TierLimits(
        name=Tier.PROFESSIONAL,
        monthly_task_limit=5_000,
        hard_block=False,
        overage_per_1k_usd=5.0,
        monthly_price_usd=79.0,
    ),
    Tier.BUSINESS: TierLimits(
        name=Tier.BUSINESS,
        monthly_task_limit=50_000,
        hard_block=False,
        overage_per_1k_usd=3.0,
        monthly_price_usd=299.0,
    ),
}


@dataclass
class TierUsage:
    """Tracks usage for a subscriber within a billing period."""
    email: str
    tier: Tier
    period_start: datetime
    period_end: datetime
    tasks_used: int = 0
    api_calls_used: int = 0

    def is_current_period(self) -> bool:
        now = datetime.now(UTC)
        return self.period_start <= now < self.period_end

    def reset_if_new_period(self) -> bool:
        """Resets counters if we're in a new billing period. Returns True if reset."""
        if not self.is_current_period():
            # Move to next period
            period_length = self.period_end - self.period_start
            self.period_start = self.period_end
            self.period_end = self.period_start + period_length
            self.tasks_used = 0
            self.api_calls_used = 0
            return True
        return False


class TierManager:
    """Manages tier enforcement and usage tracking."""

    def __init__(self, store=None):
        self.store = store  # Optional event store for persistence
        self._usage_cache: dict[str, TierUsage] = {}  # email -> TierUsage
        # Postgres pool lifted from the event store (same pattern as
        # TenantService): when present, usage counters live in
        # vex_tier_usage and survive restarts / multi-worker deploys.
        self._pg = getattr(store, "_pool", None)
        # Serializes check+record on the in-memory path: without it two
        # concurrent requests can both pass check_task_allowed and both
        # record, exceeding even the Community hard block.
        self._enforce_lock = threading.Lock()

    @staticmethod
    def _period_start(now: datetime | None = None) -> datetime:
        now = now or datetime.now(UTC)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def get_limits(self, tier: Tier) -> TierLimits:
        return TIER_DEFINITIONS[tier]

    def get_tier_from_plan(self, plan: str) -> Tier:
        """Maps a billing plan/product name to an internal tier."""
        plan_lower = plan.lower()
        if plan_lower in ("professional", "pro"):
            return Tier.PROFESSIONAL
        if plan_lower in ("business", "biz"):
            return Tier.BUSINESS
        return Tier.COMMUNITY

    def get_usage(self, email: str, tier: Tier) -> TierUsage:
        """Gets or creates usage tracking for a subscriber."""
        if self._pg is not None:
            return self._pg_usage(email, tier)
        cache_key = f"{email}:{tier.value}"
        if cache_key not in self._usage_cache:
            period_start = self._period_start()
            period_end = (period_start + timedelta(days=32)).replace(day=1)
            self._usage_cache[cache_key] = TierUsage(
                email=email,
                tier=tier,
                period_start=period_start,
                period_end=period_end,
            )
        usage = self._usage_cache[cache_key]
        usage.reset_if_new_period()
        return usage

    def _pg_usage(self, email: str, tier: Tier) -> TierUsage:
        """Reads the current period's counters from vex_tier_usage."""
        period_start = self._period_start()
        period_end = (period_start + timedelta(days=32)).replace(day=1)
        usage = TierUsage(email=email, tier=tier,
                          period_start=period_start, period_end=period_end)
        with self._pg.connection() as conn:
            row = conn.execute(
                "SELECT tasks_used, api_calls_used FROM vex_tier_usage "
                "WHERE email = %s AND tier = %s AND period_start = %s",
                (email, tier.value, period_start.date())).fetchone()
        if row:
            usage.tasks_used, usage.api_calls_used = int(row[0]), int(row[1])
        return usage

    def _pg_consume_task(self, email: str, plan: str) -> tuple[bool, str | None]:
        """Atomically checks the limit AND increments — one conditional
        upsert, so the hard block holds across workers and restarts."""
        tier = self.get_tier_from_plan(plan)
        limits = self.get_limits(tier)
        period_start = self._period_start()
        with self._pg.connection() as conn:
            if limits.hard_block:
                row = conn.execute(
                    "INSERT INTO vex_tier_usage "
                    "(email, tier, period_start, tasks_used) "
                    "VALUES (%s, %s, %s, 1) "
                    "ON CONFLICT (email, tier, period_start) DO UPDATE "
                    "SET tasks_used = vex_tier_usage.tasks_used + 1 "
                    "WHERE vex_tier_usage.tasks_used < %s RETURNING tasks_used",
                    (email, tier.value, period_start.date(),
                     limits.monthly_task_limit)).fetchone()
            else:
                row = conn.execute(
                    "INSERT INTO vex_tier_usage "
                    "(email, tier, period_start, tasks_used) "
                    "VALUES (%s, %s, %s, 1) "
                    "ON CONFLICT (email, tier, period_start) DO UPDATE "
                    "SET tasks_used = vex_tier_usage.tasks_used + 1 "
                    "RETURNING tasks_used",
                    (email, tier.value, period_start.date())).fetchone()
        if row is None:
            # Hard-blocked: the row exists at the limit.
            used = self._pg_usage(email, tier).tasks_used
            reason = (
                f"Tier {tier.value} limit reached: {used}/"
                f"{limits.monthly_task_limit} tasks this month. "
                f"Upgrade to continue.")
            log_audit_event(
                "tier_limit_exceeded", email, "tier_enforcement",
                "create_task", "denied",
                {"tier": tier.value, "tasks_used": used,
                 "limit": limits.monthly_task_limit, "hard_block": True},
                get_correlation_id())
            return False, reason

        tasks_used = int(row[0])
        if not limits.hard_block and tasks_used > limits.monthly_task_limit:
            overage = tasks_used - limits.monthly_task_limit
            log_audit_event(
                "tier_overage", email, "tier_enforcement",
                "create_task", "allowed_with_overage",
                {"tier": tier.value, "tasks_used": tasks_used,
                 "limit": limits.monthly_task_limit,
                 "overage_tasks": overage,
                 "overage_cost_usd": round(
                     (overage / 1000.0) * limits.overage_per_1k_usd, 2)},
                get_correlation_id())
        log_audit_event(
            "task_recorded", email, "tier_usage", "increment", "success",
            {"tier": tier.value, "tasks_used": tasks_used,
             "limit": limits.monthly_task_limit},
            get_correlation_id())
        return True, None

    def check_task_allowed(self, email: str, plan: str) -> tuple[bool, str | None]:
        """
        Checks if a new task is allowed for the subscriber.
        Returns (allowed, reason_if_denied).
        """
        tier = self.get_tier_from_plan(plan)
        limits = self.get_limits(tier)
        usage = self.get_usage(email, tier)

        if limits.hard_block and usage.tasks_used >= limits.monthly_task_limit:
            reason = (
                f"Tier {tier.value} limit reached: {usage.tasks_used}/{limits.monthly_task_limit} "
                f"tasks this month. Upgrade to continue."
            )
            log_audit_event(
                "tier_limit_exceeded",
                email,
                "tier_enforcement",
                "create_task",
                "denied",
                {
                    "tier": tier.value,
                    "tasks_used": usage.tasks_used,
                    "limit": limits.monthly_task_limit,
                    "hard_block": True,
                },
                get_correlation_id(),
            )
            return False, reason

        if not limits.hard_block and usage.tasks_used >= limits.monthly_task_limit:
            overage = usage.tasks_used - limits.monthly_task_limit
            overage_cost = (overage / 1000.0) * limits.overage_per_1k_usd
            log_audit_event(
                "tier_overage",
                email,
                "tier_enforcement",
                "create_task",
                "allowed_with_overage",
                {
                    "tier": tier.value,
                    "tasks_used": usage.tasks_used,
                    "limit": limits.monthly_task_limit,
                    "overage_tasks": overage,
                    "overage_cost_usd": round(overage_cost, 2),
                },
                get_correlation_id(),
            )
            # Soft block: allow but log overage

        return True, None

    def consume_task(self, email: str, plan: str) -> tuple[bool, str | None]:
        """Atomically checks the limit AND records the task.

        check_task_allowed + record_task as separate calls is a TOCTOU:
        concurrent creates can both pass the check and both increment,
        breaking the hard block. On Postgres this is a single conditional
        upsert; in-memory it holds the enforcement lock across both.
        """
        if self._pg is not None:
            return self._pg_consume_task(email, plan)
        with self._enforce_lock:
            allowed, reason = self.check_task_allowed(email, plan)
            if allowed:
                self.record_task(email, plan)
            return allowed, reason

    def record_task(self, email: str, plan: str) -> None:
        """Records a task execution for usage tracking."""
        tier = self.get_tier_from_plan(plan)
        if self._pg is not None:
            period_start = self._period_start()
            with self._pg.connection() as conn:
                row = conn.execute(
                    "INSERT INTO vex_tier_usage "
                    "(email, tier, period_start, tasks_used) "
                    "VALUES (%s, %s, %s, 1) "
                    "ON CONFLICT (email, tier, period_start) DO UPDATE "
                    "SET tasks_used = vex_tier_usage.tasks_used + 1 "
                    "RETURNING tasks_used",
                    (email, tier.value, period_start.date())).fetchone()
            tasks_used = int(row[0]) if row else 0
            log_audit_event(
                "task_recorded", email, "tier_usage", "increment",
                "success",
                {"tier": tier.value, "tasks_used": tasks_used,
                 "limit": TIER_DEFINITIONS[tier].monthly_task_limit},
                get_correlation_id())
            return
        usage = self.get_usage(email, tier)
        usage.tasks_used += 1

        log_audit_event(
            "task_recorded",
            email,
            "tier_usage",
            "increment",
            "success",
            {
                "tier": tier.value,
                "tasks_used": usage.tasks_used,
                "limit": TIER_DEFINITIONS[tier].monthly_task_limit,
            },
            get_correlation_id(),
        )

    def get_usage_summary(self, email: str, plan: str) -> dict:
        """Returns usage summary for a subscriber."""
        tier = self.get_tier_from_plan(plan)
        limits = self.get_limits(tier)
        usage = self.get_usage(email, tier)

        return {
            "email": email,
            "tier": tier.value,
            "period_start": usage.period_start.isoformat(),
            "period_end": usage.period_end.isoformat(),
            "tasks_used": usage.tasks_used,
            "task_limit": limits.monthly_task_limit,
            "hard_block": limits.hard_block,
            "overage_tasks": limits.overage_tasks(usage.tasks_used),
            "overage_cost_usd": round(limits.overage_cost_usd(usage.tasks_used), 2),
            "monthly_price_usd": limits.monthly_price_usd,
        }


# Global tier manager instance
_tier_manager: TierManager | None = None


def get_tier_manager(store=None) -> TierManager:
    global _tier_manager
    if _tier_manager is None:
        _tier_manager = TierManager(store=store)
    return _tier_manager


def set_tier_manager(manager: TierManager) -> None:
    global _tier_manager
    _tier_manager = manager