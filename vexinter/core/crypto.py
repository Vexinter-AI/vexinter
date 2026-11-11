from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

GENESIS = "0" * 64


def _json_default(o):
    """Deterministic fallback for hash_material/payload serialization.

    Anything not listed raises TypeError: silently coercing an unknown
    type would make the canonical form depend on the runtime."""
    if isinstance(o, datetime):
        return o.astimezone(UTC).isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (bytes, bytearray, memoryview)):
        # Tagged so bytes never collide with a plain string.
        return {"__type__": "bytes",
                "b64": base64.b64encode(bytes(o)).decode("ascii")}
    if isinstance(o, (set, frozenset)):
        return sorted(canonical_json(x) for x in o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not deterministically serializable: {type(o)!r}")


def canonical_json(obj) -> str:
    """Canonical serialization: sorted keys, no spaces."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, default=_json_default)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_payload(payload) -> str:
    """Content fingerprint (§3.1). None is treated as an empty dict."""
    return sha256_hex(canonical_json(payload if payload is not None else {}))
