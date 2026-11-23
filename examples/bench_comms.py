"""Secure-communication benchmark: authenticated, intact, confidential
agent-to-agent channel under adversarial delivery attempts.

Deterministic, no LLM. Two arms per attack class:

- ``raw``: E2E encryption only. The recipient tries ``decrypt_body``
  with no identity, signature, replay, timestamp or authorization
  checks. This is the baseline an "encrypt-and-send" channel gives you.
- ``vexinter``: the full ``MessageService`` path (``send`` +
  ``receive``): credential validity, binding, Ed25519 signature,
  ciphertext hash, timestamp window, anti-replay sequence, per-pair
  authorization, and rejection auditing.

Attack classes map to the rejection reasons in
``vexinter.identity.messaging``. Ground truth is exact: a legitimate
message MUST be delivered; an attack MUST NOT deliver plaintext.

Metrics per class x arm: ``delivered`` (plaintext reached the
recipient), ``rejected`` (any exception before delivery), ``audited``
(a ``message.rejected`` event exists in the store; only the vexinter
arm can produce one).

Usage:
    VEXINTER_DEV_MODE=1 python -m examples.bench_comms --n 30
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from vexinter.core.events import EventType, new_id, now
from vexinter.core.store import EventQuery, InMemoryEventStore
from vexinter.identity import (
    AgentKeyMaterial,
    IdentityService,
    MessageService,
    Rejection,
    RejectionReason,
    build_envelope_for,
)
from vexinter.identity.envelope import signable_material

# ── network fixture ─────────────────────────────────────────────────────


def make_net(*, clock=None):
    """Two legitimate agents (a=recipient, b=sender) plus attacker c
    whose credential is valid but not authorized to write to a/b."""
    store = InMemoryEventStore()
    ids = IdentityService(store)
    ms = MessageService(ids, store, clock=clock)
    keys: dict[str, AgentKeyMaterial] = {}
    creds: dict[str, object] = {}
    ids.enroll_agent("a", allowed_tools=set(), allowed_recipients={"a", "b"})
    ids.enroll_agent("b", allowed_tools=set(), allowed_recipients={"a", "b"})
    ids.enroll_agent("c", allowed_tools=set(), allowed_recipients={"d"})
    ids.enroll_agent("d", allowed_tools=set(), allowed_recipients={"c", "d"})
    for ag in ("a", "b", "c", "d"):
        inst = ids.spawn_instance(ag)
        k = AgentKeyMaterial()
        creds[ag] = ids.issue_credential(
            inst.instance_id, k.signing_pub_b64, k.encryption_pub_b64)
        keys[ag] = k
    return store, ids, ms, keys, creds


def legit_envelope(creds, keys, *, sender="b", recipient="a",
                   text="order-42", seq=1, to_agent=None):
    """A correctly built envelope: valid signature, valid encryption
    for the recipient's current key, truthful header fields."""
    return build_envelope_for(
        creds[recipient].encryption_pub,
        sender_keys=keys[sender],
        from_agent=sender,
        from_instance=creds[sender].instance_id,
        credential_id=creds[sender].credential_id,
        to_agent=to_agent or recipient,
        plaintext=text, sequence=seq)


# ── attack builders: each returns an Envelope (or a pair for replay) ──


def atk_control(net, rep):
    _, _, _, keys, creds = net
    return legit_envelope(creds, keys, text=f"order-{rep}", seq=rep + 1)


def atk_bad_signature(net, rep):
    """Envelope claims sender b but is signed by a key that was never
    registered: encryption is fine (anyone can encrypt for a), the
    signature is the only thing that can catch it."""
    _, _, _, keys, creds = net
    forger = AgentKeyMaterial()
    return build_envelope_for(
        creds["a"].encryption_pub, sender_keys=forger,
        from_agent="b", from_instance=creds["b"].instance_id,
        credential_id=creds["b"].credential_id, to_agent="a",
        plaintext="wire everything", sequence=rep + 1)


def atk_unknown_credential(net, rep):
    _, _, _, keys, creds = net
    env = legit_envelope(creds, keys, text="hi", seq=rep + 1)
    env.header.from_credential = "cred_inexistente"
    env.signature = keys["b"].sign(signable_material(env))
    return env


def atk_binding_mismatch(net, rep):
    """c's valid credential, c's valid signature, but the header claims
    the sender is b. The credential must belong to the declared sender."""
    _, _, _, keys, creds = net
    return build_envelope_for(
        creds["a"].encryption_pub, sender_keys=keys["c"],
        from_agent="b", from_instance=creds["b"].instance_id,
        credential_id=creds["c"].credential_id, to_agent="a",
        plaintext="i am b, trust me", sequence=rep + 1)


def atk_tampering(net, rep):
    """Valid envelope with one flipped byte inside the ciphertext. The
    signature covers ct, so this surfaces as a signature failure before
    any decryption is attempted."""
    import base64
    _, _, _, keys, creds = net
    env = legit_envelope(creds, keys, text="order-42", seq=rep + 1)
    raw = bytearray(base64.b64decode(env.ct))
    raw[0] ^= 0xFF
    env.ct = base64.b64encode(bytes(raw)).decode()
    env.signature = keys["b"].sign(signable_material(env))
    return env


def atk_stale_ts(net, rep):
    """Correctly signed envelope whose timestamp is one hour old:
    outside the 300s skew window."""
    _, _, _, keys, creds = net
    env = legit_envelope(creds, keys, text="order-42", seq=rep + 1)
    env.header.ts = datetime.now(UTC) - timedelta(hours=1)
    env.signature = keys["b"].sign(signable_material(env))
    return env


def atk_replay_seq(net, rep):
    """Two envelopes, same (sender, recipient) pair, same sequence
    number. The second must be rejected as replay."""
    _, _, _, keys, creds = net
    seq = rep + 1
    first = legit_envelope(creds, keys, text=f"first-{rep}", seq=seq)
    second = legit_envelope(creds, keys, text=f"second-{rep}", seq=seq)
    return (first, second)


def atk_replay_message(net, rep):
    """The exact same envelope delivered twice to the recipient."""
    _, _, _, keys, creds = net
    env = legit_envelope(creds, keys, text=f"dup-{rep}", seq=rep + 1)
    return (env, env)


def atk_unauthorized(net, rep):
    """c holds a valid credential but its identity only allows writing
    to d. Writing to a must be refused."""
    _, _, _, keys, creds = net
    return build_envelope_for(
        creds["a"].encryption_pub, sender_keys=keys["c"],
        from_agent="c", from_instance=creds["c"].instance_id,
        credential_id=creds["c"].credential_id, to_agent="a",
        plaintext="unauthorized write", sequence=rep + 1)


def atk_revoked_credential(net, rep):
    _, ids, _, keys, creds = net
    ids.revoke_credential(creds["b"].credential_id, reason="bench")
    return legit_envelope(creds, keys, text="after revoke", seq=rep + 1)


def atk_expired_credential(net, rep):
    """Credential mutated past its validity window (equivalent to
    waiting out the TTL)."""
    _, _, _, keys, creds = net
    creds["b"].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    return legit_envelope(creds, keys, text="expired", seq=rep + 1)


def atk_wrong_key(net, rep):
    """Envelope addressed to a but encrypted for c's key: passes every
    policy check, then fails at the recipient's decrypt."""
    _, _, _, keys, creds = net
    return build_envelope_for(
        creds["c"].encryption_pub, sender_keys=keys["b"],
        from_agent="b", from_instance=creds["b"].instance_id,
        credential_id=creds["b"].credential_id, to_agent="a",
        plaintext="for c not a", sequence=rep + 1)


CLASSES = {
    "control":            (True,  atk_control),
    "bad_signature":      (False, atk_bad_signature),
    "unknown_credential": (False, atk_unknown_credential),
    "binding_mismatch":   (False, atk_binding_mismatch),
    "tampering":          (False, atk_tampering),
    "stale_ts":           (False, atk_stale_ts),
    "replay_seq":         (False, atk_replay_seq),
    "replay_message":     (False, atk_replay_message),
    "unauthorized":       (False, atk_unauthorized),
    "revoked_credential": (False, atk_revoked_credential),
    "expired_credential": (False, atk_expired_credential),
    "wrong_key":          (False, atk_wrong_key),
}


# ── arms ───────────────────────────────────────────────────────────────


def run_vexinter(net, payload):
    """Full MessageService path. Returns (delivered, reason|None)."""
    store, ids, ms, keys, creds = net
    envs = payload if isinstance(payload, tuple) else (payload,)
    try:
        first = ms.send(envs[0])
        if len(envs) == 2:
            # Replay attempt: the duplicate must be refused.
            try:
                ms.send(envs[1])
            except Rejection as r:
                return False, r.reason.value
            # send() accepted the replay: try receive anyway to see if
            # the recipient-side dedup catches it.
            try:
                ms.receive(keys["a"], envs[1])
            except Rejection as r:
                return False, r.reason.value
            except Exception:
                return False, "crypto"
            return True, None
        pt = ms.receive(keys["a"], first)
        return pt is not None, None
    except Rejection as r:
        return False, r.reason.value
    except Exception as e:
        return False, type(e).__name__


def run_raw(net, payload):
    """Encryption-only baseline: the recipient attempts to decrypt,
    with no identity, signature, replay, timestamp or authorization
    layer at all."""
    _, _, _, keys, _ = net
    envs = payload if isinstance(payload, tuple) else (payload,)
    delivered = 0
    for env in envs:
        try:
            keys["a"].decrypt_body(env)
            delivered += 1
        except Exception:
            pass
    # For single messages: delivered iff decrypt worked. For replays:
    # a raw channel delivers EVERY copy (no dedup).
    return delivered == len(envs), None


ARMS = {"raw": run_raw, "vexinter": run_vexinter}


# ── runner ─────────────────────────────────────────────────────────────


def rejected_events(store) -> int:
    return len(store.query(EventQuery(types={EventType.MESSAGE_REJECTED})))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=30,
                    help="repetitions per class")
    ap.add_argument("--classes", default=",".join(CLASSES),
                    help="comma-separated subset of classes")
    ap.add_argument("--arms", default="raw,vexinter")
    args = ap.parse_args()

    wanted = [c.strip() for c in args.classes.split(",") if c.strip()]
    arms = [a.strip() for a in args.arms.split(",")]
    for c in wanted:
        if c not in CLASSES:
            sys.exit(f"unknown class: {c}")

    print(f"secure-comms benchmark: n={args.n} per class")
    header = (f"{'class':<20} {'arm':<9} {'n':>3} {'delivered':>9} "
              f"{'rejected':>8} {'audited':>7}  top reasons")
    print(header)
    print("-" * len(header))

    grand = {}
    for cname in wanted:
        expect_deliver, builder = CLASSES[cname]
        for arm in arms:
            fn = ARMS[arm]
            n = delivered = rejected = audited = 0
            reasons: dict[str, int] = {}
            for rep in range(args.n):
                net = make_net()
                payload = builder(net, rep)
                before = rejected_events(net[0])
                ok, reason = fn(net, payload)
                n += 1
                if ok:
                    delivered += 1
                else:
                    rejected += 1
                    if reason:
                        reasons[reason] = reasons.get(reason, 0) + 1
                if rejected_events(net[0]) > before:
                    audited += 1
            grand[(cname, arm)] = (delivered, rejected, audited, n)
            rs = ", ".join(f"{k}:{v}" for k, v in
                           sorted(reasons.items(), key=lambda x: -x[1])[:3])
            print(f"{cname:<20} {arm:<9} {n:>3} {delivered:>9} "
                  f"{rejected:>8} {audited:>7}  {rs}")

    print("-" * len(header))
    print("TOTAL (attacks only, control excluded)")
    for arm in arms:
        d = sum(grand[(c, arm)][0] for c in wanted if c != "control")
        r = sum(grand[(c, arm)][1] for c in wanted if c != "control")
        a = sum(grand[(c, arm)][2] for c in wanted if c != "control")
        n = sum(grand[(c, arm)][3] for c in wanted if c != "control")
        print(f"{'':<20} {arm:<9} {n:>3} {d:>9} {r:>8} {a:>7}")


if __name__ == "__main__":
    main()
