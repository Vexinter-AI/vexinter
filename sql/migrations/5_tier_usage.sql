-- Tier usage counters (billing enforcement). One row per
-- (subscriber email, tier, billing period); consumed atomically so the
-- hard/soft limits hold across workers and restarts.
CREATE TABLE IF NOT EXISTS vex_tier_usage (
    email         TEXT NOT NULL,
    tier          TEXT NOT NULL,
    period_start  DATE NOT NULL,
    tasks_used    INTEGER NOT NULL DEFAULT 0,
    api_calls_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (email, tier, period_start)
);
