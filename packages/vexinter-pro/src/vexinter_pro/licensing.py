"""Subscription license status client: signed JSON fetched over HTTPS.

Client side of the commercial license mechanism, shipped in the
``vexinter-pro`` package (paid plans only — the Community Edition
core does not include it).

The commercial backend publishes one small signed document per customer
(`{customer_id}.json`) containing the current plan and subscription
status. Self-hosted instances fetch it periodically, verify the Ed25519
signature with an embedded public key, cache the last valid document,
and derive the effective plan for quota enforcement.

Wire format (v2)::

    {"version": 2,
     "payload": {"customer_id": "...", "plan": "business",
                 "status": "active", "valid_until": null,
                 "issued_at": "..."},
     "signature": "<base64 Ed25519 over canonical_json(payload)> signed
                    by the document key>",
     "signer": {"public_key": "<doc key>", "not_after": null,
                "root_signature": "<Ed25519 by the vendor ROOT key over
                                   canonical_json({public_key, not_after})>"}}

Trust chain: the vendor ROOT public key is compiled into this package
(``ROOT_PUBLIC_KEY_B64``). A license document is trusted only if its
signer attestation verifies against that root — customers cannot
self-sign a paid plan. Document keys rotate by issuing a new
attestation, no wheel release needed. v1 documents (root-signed
directly, no attestation) are still accepted for backward compatibility.

Fail-open by design within a bounded window: if the document cannot be
fetched or verified, the last cached valid document is used up to
``max_stale_s``; beyond that the plan degrades to community.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from vexinter.core.crypto import canonical_json

_log = logging.getLogger(__name__)

LICENSE_VERSION = 2
# Vendor ROOT public key (Ed25519, base64). Compiled in on purpose:
# license documents are only trusted when chained to this key, so a
# customer cannot mint their own "business" license by pointing
# VEXINTER_LICENSE_BASE_URL at a self-hosted server. Rotating the root
# requires a new package release; rotating document keys does not.
ROOT_PUBLIC_KEY_B64 = "ma7+9YtW8zCXrPpP0C/L67XL7JEb6dfbCF0FDRGT7gU="
DEFAULT_TTL_S = 24 * 3600
DEFAULT_TIMEOUT_S = 5.0
# How long a stale-but-valid cached license may be served while the
# license server is unreachable. Bounds the fail-open window so a
# cancelled subscriber cannot keep a paid plan indefinitely by
# blocking the endpoint.
DEFAULT_MAX_STALE_S = 7 * 24 * 3600


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64d(data: str) -> bytes:
    return base64.b64decode(data)


def verify_document(doc: dict, public_b64: str) -> dict | None:
    """Verifies a wire document against a given public key; returns the
    payload or None if invalid. Low-level primitive — callers should
    prefer verify_license_document, which enforces the root trust chain."""
    try:
        if doc.get("version") not in (1, LICENSE_VERSION):
            return None
        payload = doc["payload"]
        signature = _b64d(doc["signature"])
        pub = Ed25519PublicKey.from_public_bytes(_b64d(public_b64))
        pub.verify(signature, canonical_json(payload).encode())
        return payload
    except (InvalidSignature, ValueError, TypeError, KeyError, binascii.Error):
        return None


def _attested_doc_key(signer: dict, root_b64: str,
                      now: datetime | None = None) -> str | None:
    """Verifies a v2 signer attestation against the vendor root key.
    Returns the attested document public key, or None."""
    try:
        doc_pub = signer["public_key"]
        not_after = signer.get("not_after")
        info = {"public_key": doc_pub, "not_after": not_after}
        root = Ed25519PublicKey.from_public_bytes(_b64d(root_b64))
        root.verify(_b64d(signer["root_signature"]),
                    canonical_json(info).encode())
        if not_after:
            until = datetime.fromisoformat(str(not_after))
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if until <= (now or datetime.now(UTC)):
                return None
        return doc_pub
    except (InvalidSignature, ValueError, TypeError, KeyError,
            binascii.Error):
        return None


def verify_license_document(doc: dict, *,
                            root_public_key_b64: str = ROOT_PUBLIC_KEY_B64,
                            now: datetime | None = None) -> dict | None:
    """Full trust-chain verification. v2 docs must carry a signer
    attestation chaining to the vendor root; v1 docs are accepted only
    when directly signed by the root key itself."""
    version = doc.get("version")
    if version == LICENSE_VERSION:
        doc_pub = _attested_doc_key(doc.get("signer") or {},
                                    root_public_key_b64, now)
        if doc_pub is None:
            return None
        return verify_document(doc, doc_pub)
    if version == 1:
        return verify_document(doc, root_public_key_b64)
    return None


def effective_plan(payload: dict, now: datetime | None = None) -> str:
    """Maps a verified payload to the plan the instance should enforce."""
    now = now or datetime.now(UTC)
    status = payload.get("status")
    if status in ("cancelled", "expired"):
        return "community"
    valid_until = payload.get("valid_until")
    if valid_until:
        try:
            until = datetime.fromisoformat(str(valid_until))
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if until <= now:
                return "community"
        except ValueError:
            return "community"
    plan = payload.get("plan")
    return plan if plan in ("professional", "business", "community") else "community"


class LicenseClient:
    """Fetches, verifies and caches the license status document."""

    def __init__(
        self,
        base_url: str,
        customer_id: str,
        *,
        token: str | None = None,
        cache_path: str | Path | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_stale_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
        root_public_key_b64: str = ROOT_PUBLIC_KEY_B64,
    ):
        self._url = f"{base_url.rstrip('/')}/{customer_id}.json"
        self._customer_id = customer_id
        # The customer id is a public identifier that travels through
        # URLs and logs — the token is the actual credential and is
        # sent as a Bearer header, never in the URL.
        self._headers = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )
        # The vendor root key is compiled in; there is deliberately no
        # env-configurable verification key — a customer-controlled
        # pubkey would let them self-sign paid licenses.
        self._root_b64 = root_public_key_b64
        self._cache_path = Path(cache_path) if cache_path else None
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        # Cap on how old the stale-but-valid cache may be when the license
        # server is unreachable. None = unlimited (documented fail-open).
        self._max_stale_s = max_stale_s
        self._transport = transport
        self._cached: tuple[float, dict] | None = None  # (fetched_at, payload)
        self._load_cache()

    def _verify(self, doc: dict) -> dict | None:
        return verify_license_document(doc,
                                       root_public_key_b64=self._root_b64)

    def _load_cache(self) -> None:
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            doc = json.loads(self._cache_path.read_text())
            payload = self._verify(doc)
            if payload is not None:
                self._cached = (self._cache_path.stat().st_mtime, payload)
        except (OSError, ValueError, TypeError, KeyError, binascii.Error):
            pass

    def _save_cache(self, doc: dict) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(doc))
        except OSError:
            pass

    def _fetch(self) -> dict | None:
        try:
            with httpx.Client(
                timeout=self._timeout_s, transport=self._transport,
                headers=self._headers,
            ) as client:
                resp = client.get(self._url)
            if resp.status_code != 200:
                return None
            doc = resp.json()
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None
        payload = self._verify(doc)
        if payload is None or payload.get("customer_id") != self._customer_id:
            return None
        self._cached = (time.time(), payload)
        self._save_cache(doc)
        return payload

    def _current_payload(self) -> dict | None:
        """Verified payload honoring TTL + bounded stale fallback."""
        cached = self._cached
        fresh = cached is not None and time.time() - cached[0] < self._ttl_s
        payload = cached[1] if fresh and cached is not None else self._fetch()
        if payload is None and self._cached is not None:
            age = time.time() - self._cached[0]
            if self._max_stale_s is None or age <= self._max_stale_s:
                _log.warning(
                    "license server unreachable: serving stale cache "
                    "(age %.0fs, customer %s) — fail-open by design",
                    age, self._customer_id)
                payload = self._cached[1]  # stale-but-valid cache (fail-open)
        return payload

    def get_plan(self) -> str | None:
        """Effective plan, or None when no license is configured/reachable."""
        payload = self._current_payload()
        if payload is None:
            return None
        return effective_plan(payload)

    def get_payload(self) -> dict | None:
        """Full verified payload (plan, status, max_instances, …) or
        None when no license is configured/reachable."""
        return self._current_payload()


def client_from_env() -> LicenseClient | None:
    """Builds a LicenseClient from VEXINTER_LICENSE_* env vars, if configured."""
    base_url = os.environ.get("VEXINTER_LICENSE_BASE_URL")
    customer_id = os.environ.get("VEXINTER_LICENSE_ID")
    token = os.environ.get("VEXINTER_LICENSE_TOKEN")
    # The token is the credential: without it the license endpoint
    # answers 401 and the instance silently stays on Community, so
    # refuse to build a client that can never succeed.
    if not (base_url and customer_id and token):
        return None
    if os.environ.get("VEXINTER_LICENSE_PUBKEY") or os.environ.get(
            "VEXINTER_LICENSE_PUBKEYS"):
        _log.warning(
            "VEXINTER_LICENSE_PUBKEY(S) is deprecated and ignored: the "
            "verification root key is embedded in the package. Remove "
            "the variable(s) from your environment.")
    cache = os.environ.get("VEXINTER_LICENSE_CACHE") or str(
        Path(tempfile.gettempdir()) / f"vexinter-license-{customer_id}.json"
    )
    max_stale = os.environ.get("VEXINTER_LICENSE_MAX_STALE_S")
    return LicenseClient(
        base_url, customer_id, token=token, cache_path=cache,
        # Bounded fail-open: without a cap a subscriber could block the
        # license endpoint and keep a stale paid plan forever. Default
        # 7 days; "unlimited" must be requested explicitly with -1.
        max_stale_s=(None if max_stale == "-1"
                     else float(max_stale) if max_stale
                     else DEFAULT_MAX_STALE_S))
