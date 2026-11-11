"""Tests for the signed license status mechanism (core/licensing.py)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vexinter.core.licensing import (
    LicenseClient,
    build_payload,
    customer_id_for_email,
    effective_plan,
    generate_keypair,
    public_key_from_private,
    sign_document,
    verify_document,
)


@pytest.fixture
def keys():
    return generate_keypair()  # (private_b64, public_b64)


def _doc_for(email, plan, status, keys, **kw):
    cid = customer_id_for_email(email)
    payload = build_payload(cid, plan, status, **kw)
    return cid, sign_document(payload, keys[0])


def test_customer_id_is_stable_and_opaque():
    a = customer_id_for_email("User@Example.com")
    b = customer_id_for_email("user@example.com ")
    assert a == b and a.startswith("lic_") and "@" not in a


def test_sign_verify_roundtrip(keys):
    _, doc = _doc_for("a@b.c", "business", "active", keys)
    payload = verify_document(doc, keys[1])
    assert payload is not None
    assert payload["plan"] == "business"


def test_tampered_payload_rejected(keys):
    _, doc = _doc_for("a@b.c", "business", "active", keys)
    doc["payload"]["plan"] = "business"  # same; now mutate status
    doc["payload"]["status"] = "cancelled"
    assert verify_document(doc, keys[1]) is None


def test_wrong_key_rejected(keys):
    _, doc = _doc_for("a@b.c", "business", "active", keys)
    other = generate_keypair()
    assert verify_document(doc, other[1]) is None


def test_effective_plan_rules():
    now = datetime.now(UTC)
    future = (now + timedelta(days=30)).isoformat()
    past = (now - timedelta(days=1)).isoformat()

    assert effective_plan({"plan": "business", "status": "active"}, now) == "business"
    assert effective_plan({"plan": "business", "status": "cancelled"}, now) == "community"
    assert effective_plan({"plan": "business", "status": "expired"}, now) == "community"
    assert effective_plan({"plan": "business", "status": "past_due"}, now) == "business"
    assert effective_plan(
        {"plan": "business", "status": "active", "valid_until": future}, now
    ) == "business"
    assert effective_plan(
        {"plan": "business", "status": "active", "valid_until": past}, now
    ) == "community"
    assert effective_plan({"plan": "bogus", "status": "active"}, now) == "community"


def _client(keys, handler, tmp_path, ttl_s=3600):
    transport = httpx.MockTransport(handler)
    return LicenseClient(
        "https://licenses.example.com",
        "lic_test",
        keys[1],
        cache_path=tmp_path / "lic.json",
        ttl_s=ttl_s,
        transport=transport,
    )


def test_client_fetches_and_applies_plan(keys, tmp_path):
    cid = "lic_test"
    payload = build_payload(cid, "business", "active")
    doc = sign_document(payload, keys[0])
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=doc)

    client = _client(keys, handler, tmp_path)
    assert client.get_plan() == "business"
    assert calls == [f"/{cid}.json"]
    # Second call within TTL does not refetch
    assert client.get_plan() == "business"
    assert len(calls) == 1


def test_client_rejects_wrong_customer(keys, tmp_path):
    payload = build_payload("lic_other", "business", "active")
    doc = sign_document(payload, keys[0])
    client = _client(keys, lambda r: httpx.Response(200, json=doc), tmp_path)
    assert client.get_plan() is None


def test_client_falls_back_to_cache_on_failure(keys, tmp_path):
    cid = "lic_test"
    doc = sign_document(build_payload(cid, "professional", "active"), keys[0])

    state = {"fail": False}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json=doc)

    client = _client(keys, handler, tmp_path, ttl_s=0)  # always refetch
    assert client.get_plan() == "professional"
    state["fail"] = True
    # TTL expired -> fetch fails -> stale cache still applies
    assert client.get_plan() == "professional"


def test_client_no_cache_no_server_returns_none(keys, tmp_path):
    client = _client(
        keys, lambda r: httpx.Response(404), tmp_path, ttl_s=0
    )
    assert client.get_plan() is None


def test_public_key_from_private(keys):
    assert public_key_from_private(keys[0]) == keys[1]


def test_rotation_window_accepts_previous_signing_key(keys, tmp_path):
    """During rotation the client trusts old+new pubkeys; an unrelated
    key is still rejected."""
    cid, doc = _doc_for("ana@x.com", "business", "active", keys)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=doc))
    # Primary = a NEW key, previous signing key passed as extra.
    _, new_pub = generate_keypair()
    client = LicenseClient(
        "https://licenses.example.com", cid, new_pub,
        cache_path=tmp_path / "lic.json", transport=transport,
        extra_public_keys=[keys[1]])
    assert client.get_plan() == "business"

    rogue_priv, _rogue_pub = generate_keypair()
    payload = build_payload(cid, "enterprise", "active")
    rogue_doc = sign_document(payload, rogue_priv)
    client2 = LicenseClient(
        "https://licenses.example.com", cid, new_pub,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=rogue_doc)),
        extra_public_keys=[keys[1]])
    assert client2.get_plan() is None
