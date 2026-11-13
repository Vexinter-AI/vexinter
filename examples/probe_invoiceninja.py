"""Invoice Ninja v5 contract probe (local laboratory).

Only talks to an instance on localhost. Does not print the token.
Serves to pin down the real contract before writing the adapter.
"""
import json
import os
import sys
import uuid

import httpx

URL = os.environ.get("INVOICENINJA_URL", "").rstrip("/")
TOKEN = os.environ.get("INVOICENINJA_API_TOKEN", "")
if not URL or not TOKEN:
    sys.exit("INVOICENINJA_URL and INVOICENINJA_API_TOKEN are missing from the environment.")
if not (URL.startswith(("http://localhost", "http://127.0.0.1"))):
    sys.exit("This probe only runs against a local laboratory instance.")

http = httpx.Client(
    base_url=f"{URL}/api/v1", timeout=30.0,
    headers={"X-API-TOKEN": TOKEN, "X-Requested-With": "XMLHttpRequest",
             "Accept": "application/json", "Content-Type": "application/json"})

KEEP = ("id", "number", "name", "status_id", "is_deleted", "archived_at",
        "amount", "balance", "paid_to_date", "client_id", "idempotency_key",
        "contacts", "line_items")


def slim(d):
    if isinstance(d, list):
        return [slim(x) for x in d]
    if not isinstance(d, dict):
        return d
    out = {}
    for k in KEEP:
        if k in d:
            v = d[k]
            if k == "contacts":
                v = [c.get("email") for c in v] if isinstance(v, list) else v
            elif k == "line_items":
                v = len(v) if isinstance(v, list) else v
            out[k] = v
    return out


def call(method, path, body=None, params=None):
    r = http.request(method, path, json=body, params=params)
    try:
        data = r.json()
    except Exception:
        data = r.text[:300]
    print(f"\n{method} {path} {params or ''} -> {r.status_code}")
    return r.status_code, data


def data_of(d):
    return d.get("data") if isinstance(d, dict) else None


def show(status, d):
    if status >= 400:
        print("  ERROR:", json.dumps(d, ensure_ascii=False)[:500])
    else:
        print("  ", json.dumps(slim(data_of(d)), ensure_ascii=False))


def need(status, d, what):
    if status >= 400:
        show(status, d)
        sys.exit(f"stopped: {what} failed")


tag = uuid.uuid4().hex[:8]
email = f"probe-{tag}@example.com"

print("== 1. authentication ==")
s, d = call("GET", "/clients", params={"per_page": 1})
need(s, d, "authentication")
print("  ok")

print("\n== 2. create customer ==")
s, d = call("POST", "/clients",
            {"name": f"Probe {tag}", "contacts": [{"email": email}]})
show(s, d)
need(s, d, "creating the customer")
cid = data_of(d)["id"]

print("\n== 3. read the just-created customer (immediate read?) ==")
s, d = call("GET", "/clients", params={"email": email})
show(s, d)
print("  found:", len(data_of(d) or []))

print("\n== 4. create invoice with idempotency_key, twice ==")
cuerpo = {"client_id": cid, "idempotency_key": f"probe-{tag}",
          "line_items": [{"notes": "probe", "cost": 100, "quantity": 1}]}
s1, d1 = call("POST", "/invoices", cuerpo)
show(s1, d1)
need(s1, d1, "creating the invoice")
iid = data_of(d1)["id"]
s2, d2 = call("POST", "/invoices", cuerpo)
show(s2, d2)
if s2 < 400:
    print("  second call returned the same id:",
          isinstance(data_of(d2), dict) and data_of(d2).get("id") == iid)
s, d = call("GET", "/invoices", params={"client_id": cid})
print("  customer invoices after both calls:", len(data_of(d) or []))
print("  ", json.dumps(slim(data_of(d)), ensure_ascii=False))

print("\n== 5. send (mark_sent) ==")
s, d = call("POST", "/invoices/bulk", {"action": "mark_sent", "ids": [iid]})
show(s, d)
s, d = call("GET", f"/invoices/{iid}")
show(s, d)

print("\n== 6. cancel ==")
s, d = call("POST", "/invoices/bulk", {"action": "cancel", "ids": [iid]})
show(s, d)
s, d = call("GET", f"/invoices/{iid}")
show(s, d)

print("\n== 7. listing filters ==")
for p in ({"client_id": cid},
          {"client_id": cid, "status": "active"},
          {"client_id": cid, "with_trashed": "true"}):
    s, d = call("GET", "/invoices", params=p)
    if s >= 400:
        show(s, d)
    else:
        print("  ", json.dumps(slim(data_of(d)), ensure_ascii=False))

print("\n== 8. archive the customer ==")
s, d = call("POST", "/clients/bulk", {"action": "archive", "ids": [cid]})
show(s, d)
for p in ({"email": email}, {"email": email, "status": "archived"}):
    s, d = call("GET", "/clients", params=p)
    if s >= 400:
        show(s, d)
    else:
        print("  ", json.dumps(slim(data_of(d)), ensure_ascii=False))

print("\ndone")
