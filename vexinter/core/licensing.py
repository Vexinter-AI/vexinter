"""Subscription license status: signed JSON fetched over HTTPS.

The commercial backend publishes one small signed document per customer
(`{customer_id}.json`) containing the current plan and subscription
status. Self-hosted instances fetch it periodically, verify the Ed25519
signature with an embedded public key, cache the last valid document,
and derive the effective plan for quota enforcement.

Wire format::

    {"version": 1,
     "payload": {"customer_id": "...", "plan": "business",
                 "status": "active", "valid_until": null,
                 "issued_at": "..."},
     "signature": "<base64 Ed25519 over canonical_json(payload)>"}

Fail-open by design: if the document cannot be fetched or verified, the
last cached valid document is used; if none exists, the caller falls
back to the configured default plan. Enforcement remains contractual —
this mechanism makes the correct default automatic and auditable.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
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
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from vexinter.core.crypto import canonical_json

_log = logging.getLogger(__name__)

LICENSE_VERSION = 1
DEFAULT_TTL_S = 24 * 3600
DEFAULT_TIMEOUT_S = 5.0


def customer_id_for_email(email: str) -> str:
    """Stable, non-PII customer identifier derived from the subscriber email."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"lic_{digest[:16]}"


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64d(data: str) -> bytes:
    return base64.b64decode(data)


def generate_keypair() -> tuple[str, str]:
    """Returns (private_b64, public_b64) for the license signing key."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_b64 = _b64e(
        priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    )
    pub_b64 = _b64e(pub.public_bytes(Encoding.Raw, PublicFormat.Raw))
    return priv_b64, pub_b64


def public_key_from_private(private_b64: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(_b64d(private_b64))
    return _b64e(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def sign_document(payload: dict, private_b64: str) -> dict:
    """Builds the signed wire document for a license payload."""
    priv = Ed25519PrivateKey.from_private_bytes(_b64d(private_b64))
    signature = priv.sign(canonical_json(payload).encode())
    return {
        "version": LICENSE_VERSION,
        "payload": payload,
        "signature": _b64e(signature),
    }


def verify_document(doc: dict, public_b64: str) -> dict | None:
    """Verifies a wire document; returns the payload or None if invalid."""
    try:
        if doc.get("version") != LICENSE_VERSION:
            return None
        payload = doc["payload"]
        signature = _b64d(doc["signature"])
        pub = Ed25519PublicKey.from_public_bytes(_b64d(public_b64))
        pub.verify(signature, canonical_json(payload).encode())
        return payload
    except (InvalidSignature, ValueError, TypeError, KeyError, binascii.Error):
        return None


def build_payload(
    customer_id: str,
    plan: str,
    status: str,
    valid_until: str | None = None,
) -> dict:
    return {
        "customer_id": customer_id,
        "plan": plan,
        "status": status,
        "valid_until": valid_until,
        "issued_at": datetime.now(UTC).isoformat(),
        "version": LICENSE_VERSION,
    }


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
        public_key_b64: str,
        *,
        cache_path: str | Path | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_stale_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
        extra_public_keys: list[str] | None = None,
    ):
        self._url = f"{base_url.rstrip('/')}/{customer_id}.json"
        self._customer_id = customer_id
        # Accepted verification keys: the active one plus any previous
        # keys kept during a rotation window (VEXINTER_LICENSE_PUBKEYS).
        self._pubkeys = [public_key_b64, *(extra_public_keys or [])]
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
        """Verifies against any accepted key (rotation window support)."""
        for pubkey in self._pubkeys:
            payload = verify_document(doc, pubkey)
            if payload is not None:
                return payload
        return None

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
                timeout=self._timeout_s, transport=self._transport
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

    def get_plan(self) -> str | None:
        """Effective plan, or None when no license is configured/reachable."""
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
        if payload is None:
            return None
        return effective_plan(payload)


def client_from_env() -> LicenseClient | None:
    """Builds a LicenseClient from VEXINTER_LICENSE_* env vars, if configured."""
    base_url = os.environ.get("VEXINTER_LICENSE_BASE_URL")
    customer_id = os.environ.get("VEXINTER_LICENSE_ID")
    pubkey = os.environ.get("VEXINTER_LICENSE_PUBKEY")
    if not (base_url and customer_id and pubkey):
        return None
    cache = os.environ.get("VEXINTER_LICENSE_CACHE") or str(
        Path(tempfile.gettempdir()) / f"vexinter-license-{customer_id}.json"
    )
    max_stale = os.environ.get("VEXINTER_LICENSE_MAX_STALE_S")
    # Comma-separated additional public keys accepted during a signing-key
    # rotation: deploy the new key as VEXINTER_LICENSE_PUBKEY while keeping
    # the old one(s) here until every customer has fetched a re-signed doc.
    extra = [k.strip() for k in
             os.environ.get("VEXINTER_LICENSE_PUBKEYS", "").split(",")
             if k.strip()]
    return LicenseClient(
        base_url, customer_id, pubkey, cache_path=cache,
        max_stale_s=float(max_stale) if max_stale else None,
        extra_public_keys=extra or None)
