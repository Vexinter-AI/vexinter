"""Vexinter service for the TypeScript SDK end-to-end tests.

Usage: python packages/sdk-ts/scripts/serve_test.py PORT [fail]
In-memory store, demo adapters and a fixed admin token FOR TESTS ONLY.
Listens on 127.0.0.1 only. With `fail`, the first
create_invoice responds ok without persisting (fake success) and billing is
not idempotent: the case ends in a human decision.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

# services.app builds a module-level FastAPI app at import time, which
# requires VEXINTER_ADMIN_TOKEN outside dev mode. Set a throwaway one so
# the import succeeds; the real test app below passes admin_token
# explicitly. setdefault keeps a real env value if present.
os.environ.setdefault(
    "VEXINTER_ADMIN_TOKEN", "e2e-module-level-token-0123456789abcdef")

import uvicorn

from services.app import make_app
from vexinter.core.store import InMemoryEventStore
from vexinter.demo import BillingSystem, CrmSystem

ADMIN = "tok-e2e-sdk-ts-1234"
FAIL = "fail" in sys.argv[2:]

billing = (BillingSystem(fail_first=1, supports_idempotency=False)
           if FAIL else BillingSystem())
app = make_app(store=InMemoryEventStore(),
               adapters=[CrmSystem(), billing],
               dev_mode=False, admin_token=ADMIN)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]),
                log_level="warning")
