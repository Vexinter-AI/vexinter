"""Probe: does Invoice Ninja allow cancelling a DRAFT? (local laboratory)"""
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

tag = uuid.uuid4().hex[:8]
r = http.post("/clients", json={"name": f"Draft {tag}",
                                "contacts": [{"email": f"b-{tag}@example.com"}]})
cid = r.json()["data"]["id"]
r = http.post("/invoices", json={
    "client_id": cid,
    "line_items": [{"notes": "probe", "cost": 100, "quantity": 1}]})
iid = r.json()["data"]["id"]
print("draft created, status_id =", r.json()["data"]["status_id"])

r = http.post("/invoices/bulk", json={"action": "cancel", "ids": [iid]})
print("cancel on draft -> HTTP", r.status_code)
cuerpo = r.json() if r.content else None
if r.status_code == 200 and isinstance(cuerpo, dict):
    print("  status_id after cancelling:", cuerpo["data"][0].get("status_id"))
else:
    print("  response:", str(cuerpo)[:300])

r = http.get(f"/invoices/{iid}")
print("final state queried -> status_id =", r.json()["data"]["status_id"])
