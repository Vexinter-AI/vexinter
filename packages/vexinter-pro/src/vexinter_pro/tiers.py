"""Paid-tier limits registered into the Community core at import time.

These mirror TERMS-OF-SERVICE.md. All tiers hard-block at their
monthly quota — overage billing is not implemented.
"""

from vexinter.core.tiers import Tier, TierLimits, register_tier_limits

PAID_TIER_LIMITS: tuple[TierLimits, ...] = (
    TierLimits(
        name=Tier.PROFESSIONAL,
        monthly_task_limit=5_000,
        hard_block=True,
        overage_per_1k_usd=0.0,
        monthly_price_usd=79.0,
    ),
    TierLimits(
        name=Tier.BUSINESS,
        monthly_task_limit=50_000,
        hard_block=True,
        overage_per_1k_usd=0.0,
        monthly_price_usd=299.0,
    ),
)

for _limits in PAID_TIER_LIMITS:
    register_tier_limits(_limits)
