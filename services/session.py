"""Shared session/auth helpers for the admin SPA.

The SPA login issues an HMAC-signed HttpOnly cookie; these helpers sign,
verify and rate-limit it. Extracted from the legacy Jinja console, which
was removed — the SPA at /app is the only UI.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import Request

SESSION_TTL_S = 8 * 3600
COOKIE = "vex_console"


class LoginLimiter:
    """Rate limit for failed access attempts, per origin.

    After `max_fails` failures within `window_s` seconds, that origin is
    locked for `lock_s` seconds (even with the correct token). It is per
    origin and in memory: behind a reverse proxy all accesses share the
    proxy's address, and a restart clears it.
    """

    def __init__(self, max_fails: int = 5, window_s: float = 900.0,
                 lock_s: float = 900.0, clock=time.monotonic) -> None:
        self._max, self._window, self._lock_s = max_fails, window_s, lock_s
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    def blocked(self, key: str) -> float:
        """Seconds remaining before that origin can try again."""
        remaining = self._locked.get(key, 0.0) - self._clock()
        if remaining <= 0:
            self._locked.pop(key, None)
            return 0.0
        return remaining

    def failure(self, key: str) -> None:
        t = self._clock()
        vivos = [x for x in self._fails.get(key, []) if t - x < self._window]
        vivos.append(t)
        self._fails[key] = vivos
        if len(vivos) >= self._max:
            self._locked[key] = t + self._lock_s
            self._fails.pop(key, None)
        if len(self._fails) > 10_000:                # don't grow unboundedly
            self._fails = {k: v for k, v in self._fails.items()
                           if v and t - v[-1] < self._window}

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked.pop(key, None)


def _sign(token: str, name: str, exp: int) -> str:
    return hmac.new(token.encode(), f"{name}.{exp}".encode(),
                    hashlib.sha256).hexdigest()


def _issue(token: str, name: str) -> str:
    """Session cookie: name.expiry.signature, signed with THAT
    operator's token; it cannot be presented as someone else's."""
    exp = int(time.time()) + SESSION_TTL_S
    return f"{name}.{exp}.{_sign(token, name, exp)}"


def _session(tokens_by_name: dict[str, str], value: str | None) -> str | None:
    """Operator name of a valid cookie, or None."""
    if not value:
        return None
    partes = value.split(".")
    if len(partes) != 3:
        return None
    name, exp, sig = partes
    token = tokens_by_name.get(name)
    if token is None or not exp.isdigit() or int(exp) < time.time():
        return None
    if not hmac.compare_digest(_sign(token, name, int(exp)), sig):
        return None
    return name


def preview_case(rt, case_id: str) -> dict | None:
    """Queries the CURRENT state of a pending case, without executing anything.

    No verdict is recorded (record=False): looking at the queue does not
    generate spurious evidence; the recording re-verification is done by decide().
    """
    case = rt.human_queue.get_case(case_id)
    if case is None:
        return None
    run = rt.runs.get(case.task_id)
    if run is None:
        return {"case_id": case_id, "status_now": "unknown",
                "would_skip_execution": False,
                "detail": "the task is no longer in memory (restart pending)"}
    v = rt.verifier.verify(run, record=False)
    return {"case_id": case_id, "case_status": case.status.value,
            "status_now": v.status,
            "provisional": v.provisional,
            "would_skip_execution": v.status == "met",
            "detail": v.detail}


_TRUSTED_PROXIES: list | None = None


def _trusted_networks():
    global _TRUSTED_PROXIES
    if _TRUSTED_PROXIES is None:
        import ipaddress
        nets = []
        for cidr in os.environ.get(
                "VEXINTER_TRUSTED_PROXIES", "").split(","):
            cidr = cidr.strip()
            if cidr:
                try:
                    nets.append(ipaddress.ip_network(cidr, strict=False))
                except ValueError:
                    pass
        _TRUSTED_PROXIES = nets
    return _TRUSTED_PROXIES


def client_ip(request: Request) -> str:
    """Client IP for rate limiting.

    X-Forwarded-For is trusted when the direct peer is private/loopback
    (same-host/LAN proxy) OR inside a CIDR listed in
    VEXINTER_TRUSTED_PROXIES, needed for cloud LBs (ALB, Cloudflare)
    whose egress IPs are public. A public client cannot spoof XFF
    unless it can also spoof its source address."""
    import ipaddress
    host = (request.client.host if request.client else "").strip("[]")
    try:
        peer = ipaddress.ip_address(host)
    except ValueError:
        return host or "?"
    trusted = peer.is_private or peer.is_loopback or any(
        peer in net for net in _trusted_networks())
    fwd = request.headers.get("x-forwarded-for")
    if fwd and trusted:
        return fwd.split(",", 1)[0].strip() or host
    return host or "?"
