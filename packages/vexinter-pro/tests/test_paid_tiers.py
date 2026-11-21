"""Paid-tier registration: importing vexinter_pro adds Professional and
Business limits to the core registry."""

import vexinter_pro  # noqa: F401 — import performs the registration
from vexinter.core.tiers import TIER_DEFINITIONS, Tier, TierManager


def test_paid_tiers_registered():
    pro = TIER_DEFINITIONS[Tier.PROFESSIONAL]
    biz = TIER_DEFINITIONS[Tier.BUSINESS]
    assert pro.monthly_task_limit == 5_000 and pro.hard_block is True
    assert biz.monthly_task_limit == 50_000 and biz.hard_block is True


def test_paid_plan_enforces_its_own_quota():
    mgr = TierManager()
    limits = mgr.get_limits(Tier.PROFESSIONAL)
    assert limits.monthly_task_limit == 5_000
    assert limits.monthly_price_usd == 79.0


def test_client_from_env_passthrough(monkeypatch):
    assert vexinter_pro.client_from_env() is None
    monkeypatch.setenv("VEXINTER_LICENSE_BASE_URL", "https://lic.example.com")
    monkeypatch.setenv("VEXINTER_LICENSE_ID", "lic_abc")
    monkeypatch.setenv("VEXINTER_LICENSE_TOKEN", "tok_abc")
    client = vexinter_pro.client_from_env()
    assert client is not None


def test_client_from_env_ignores_pubkey_override(monkeypatch):
    """VEXINTER_LICENSE_PUBKEY is deprecated: the verification root is
    embedded, so a customer-supplied key must not become trusted."""
    monkeypatch.setenv("VEXINTER_LICENSE_BASE_URL", "https://lic.example.com")
    monkeypatch.setenv("VEXINTER_LICENSE_ID", "lic_abc")
    monkeypatch.setenv("VEXINTER_LICENSE_TOKEN", "tok_abc")
    monkeypatch.setenv("VEXINTER_LICENSE_PUBKEY", "AAAA")
    client = vexinter_pro.client_from_env()
    assert client is not None
    # The client still verifies against the embedded vendor root, not
    # the env-provided key.
    assert client._root_b64 != "AAAA"
