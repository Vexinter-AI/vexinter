"""Tests for vexinter_pro.licensing — the paid license-status client.

Test documents are signed locally with throwaway Ed25519 keys following
the v2 wire format: payload signed by a document key, document key
attested by a root key (which plays the role of the embedded vendor
root in these tests).
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from vexinter.core.crypto import canonical_json
from vexinter_pro.licensing import (
    LicenseClient,
    effective_plan,
    verify_document,
    verify_license_document,
)


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    b64e = lambda b: base64.b64encode(b).decode()
    return (
        b64e(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())),
        b64e(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)),
    )


def _sign_v1(payload: dict, priv_b64: str) -> dict:
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    sig = priv.sign(canonical_json(payload).encode())
    return {
        "version": 1,
        "payload": payload,
        "signature": base64.b64encode(sig).decode(),
    }


def _sign_v2(payload: dict, doc_priv: str, doc_pub: str,
             root_priv: str, not_after=None) -> dict:
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(doc_priv))
    sig = priv.sign(canonical_json(payload).encode())
    signer_info = {"public_key": doc_pub, "not_after": not_after}
    root = Ed25519PrivateKey.from_private_bytes(base64.b64decode(root_priv))
    root_sig = root.sign(canonical_json(signer_info).encode())
    return {
        "version": 2,
        "payload": payload,
        "signature": base64.b64encode(sig).decode(),
        "signer": {
            **signer_info,
            "root_signature": base64.b64encode(root_sig).decode(),
        },
    }


def _payload(cid, plan, status, **kw):
    return {
        "customer_id": cid, "plan": plan, "status": status,
        "valid_until": kw.get("valid_until"),
        "issued_at": datetime.now(UTC).isoformat(),
        "version": 2,
    }


@pytest.fixture
def keys():
    """(root_priv, root_pub, doc_priv, doc_pub)"""
    root_priv, root_pub = _keypair()
    doc_priv, doc_pub = _keypair()
    return root_priv, root_pub, doc_priv, doc_pub


def _doc(keys, cid="lic_x", plan="business", status="active", **kw):
    return _sign_v2(_payload(cid, plan, status, **kw), keys[2], keys[3],
                    keys[0])


def test_sign_verify_roundtrip(keys):
    payload = verify_license_document(_doc(keys),
                                      root_public_key_b64=keys[1])
    assert payload is not None and payload["plan"] == "business"


def test_tampered_payload_rejected(keys):
    doc = _doc(keys)
    doc["payload"]["status"] = "cancelled"
    assert verify_license_document(doc,
                                   root_public_key_b64=keys[1]) is None


def test_wrong_doc_key_rejected(keys):
    doc = _sign_v2(_payload("lic_x", "business", "active"),
                   _keypair()[0], keys[3], keys[0])
    assert verify_license_document(doc,
                                   root_public_key_b64=keys[1]) is None


def test_forged_root_attestation_rejected(keys):
    """A customer-generated keypair cannot attest itself: the root
    signature must chain to the vendor root."""
    attacker_priv, attacker_pub = _keypair()
    doc = _sign_v2(_payload("lic_x", "business", "active"),
                   attacker_priv, attacker_pub, attacker_priv)
    assert verify_license_document(doc,
                                   root_public_key_b64=keys[1]) is None


def test_v1_doc_accepted_only_if_root_signed(keys):
    payload = _payload("lic_x", "business", "active")
    # v1 signed by the root itself: accepted (legacy docs).
    assert verify_license_document(
        _sign_v1(payload, keys[0]),
        root_public_key_b64=keys[1]) is not None
    # v1 signed by a doc key: rejected — v1 has no attestation channel.
    assert verify_license_document(
        _sign_v1(payload, keys[2]),
        root_public_key_b64=keys[1]) is None


def test_signer_not_after_expiry(keys):
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    doc = _sign_v2(_payload("lic_x", "business", "active"),
                   keys[2], keys[3], keys[0], not_after=past)
    assert verify_license_document(doc,
                                   root_public_key_b64=keys[1]) is None


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


def _client(keys, handler, tmp_path, ttl_s=3600, **kw):
    return LicenseClient(
        "https://licenses.example.com", "lic_test",
        cache_path=tmp_path / "lic.json", ttl_s=ttl_s,
        transport=httpx.MockTransport(handler),
        root_public_key_b64=keys[1], **kw)


def test_client_fetches_and_applies_plan(keys, tmp_path):
    doc = _doc(keys, cid="lic_test")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=doc)

    client = _client(keys, handler, tmp_path)
    assert client.get_plan() == "business"
    assert calls == ["/lic_test.json"]
    assert client.get_plan() == "business"
    assert len(calls) == 1  # TTL hit, no refetch


def test_client_rejects_wrong_customer(keys, tmp_path):
    doc = _doc(keys, cid="lic_other")
    client = _client(keys, lambda r: httpx.Response(200, json=doc), tmp_path)
    assert client.get_plan() is None


def test_client_falls_back_to_cache_on_failure(keys, tmp_path):
    doc = _doc(keys, cid="lic_test", plan="professional")
    state = {"fail": False}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json=doc)

    client = _client(keys, handler, tmp_path, ttl_s=0, max_stale_s=3600)
    assert client.get_plan() == "professional"
    state["fail"] = True
    assert client.get_plan() == "professional"  # stale cache within bound


def test_stale_cache_beyond_max_stale_degrades(keys, tmp_path):
    doc = _doc(keys, cid="lic_test")
    state = {"fail": False}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json=doc)

    client = _client(keys, handler, tmp_path, ttl_s=0, max_stale_s=0)
    assert client.get_plan() == "business"
    state["fail"] = True
    # max_stale_s=0: any positive age exceeds the bound -> None -> community
    assert client.get_plan() is None


def test_client_no_cache_no_server_returns_none(keys, tmp_path):
    client = _client(keys, lambda r: httpx.Response(404), tmp_path, ttl_s=0)
    assert client.get_plan() is None


def test_doc_key_rotation_via_attestation(keys, tmp_path):
    """A rotated document key works without changing the client: the
    new key's attestation is verified against the same root."""
    new_doc_priv, new_doc_pub = _keypair()
    doc = _sign_v2(_payload("lic_test", "business", "active"),
                   new_doc_priv, new_doc_pub, keys[0])
    client = _client(keys, lambda r: httpx.Response(200, json=doc), tmp_path)
    assert client.get_plan() == "business"
