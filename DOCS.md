# Vexinter Developer Documentation

Vexinter is a supervision and verification layer for AI agents. Every
operational action an agent takes goes through a declared, auditable
pipeline: tools are invoked under policy, results are checked against a
declarative condition, and the whole run is recorded on a hash-chained
event store. Agent-to-agent messages are signed and end-to-end
encrypted.

This document covers installation, the task lifecycle, quotas and
guardrails, the SDKs, the HTTP API and configuration. It is written for
developers integrating Vexinter and for customers running it.

---

## Requirements

- **Python**: 3.11 or newer for the Python SDK and local runtime.
- **Community edition**: `pip install vexinter` from PyPI (source
  available, non-commercial use).
- **Service**: PostgreSQL 16+ and Docker — `docker compose up --build`
  brings up the API and its dependencies.
- **TypeScript SDK**: Node.js 20+ and `npm install @vexinter-ai/sdk`
  (MIT licensed, safe to embed in any product).

---

## Editions

Vexinter is source-available:

| Edition | Source | Plans |
| --- | --- | --- |
| **Community** | Public repo (`Vexinter-AI/vexinter`) | Community tier: 500 tasks/month, hard block |
| **Pro** | Private package `vexinter-pro` | Professional (5,000 tasks/month) and Business (50,000 tasks/month), both hard-blocked at the limit |

The public core contains only the Community tier and a registration
hook. Paid tiers and the signed-license client ship in `vexinter-pro`,
which registers itself at import time, the paid **features** and the
**license validation** code are simply not present in the public
repository, so a paid plan cannot be activated from the public tree
alone. Note that the Community task cap itself is a constant in the
public source: like any source-available project, the license, not
the code, is the legal enforcement against raising it.

### Installing the Pro package

Subscribers receive a customer id (`lic_…`, a **public identifier**)
and a secret **license token** in the welcome email. Two different
things, two different channels:

- The **token** authenticates every request to the license and
  package endpoints, it travels in headers (`.netrc`/`Authorization`),
  never in URLs, so it does not end up in shell history, CI logs or
  proxy logs.
- The **customer id** only identifies *which* license to check; it is
  not a credential.

License enforcement, add to `.env`:

```bash
VEXINTER_LICENSE_BASE_URL=https://billing.vexinter.com/license
VEXINTER_LICENSE_ID=lic_xxxxxxxxxxxxxxxx
VEXINTER_LICENSE_TOKEN=<token from the welcome email>
```

The Ed25519 **root public key is compiled into `vexinter-pro`**, you
never configure a public key, so a self-hosted license server cannot
forge a paid plan. `VEXINTER_LICENSE_PUBKEY`/`PUBKEYS` are deprecated
and ignored.

Wheel download, add to `~/.netrc` (`chmod 600`) so pip authenticates
without the token touching your shell history:

```
machine billing.vexinter.com login lic_xxxxxxxxxxxxxxxx password <token>
```

```bash
# The exact wheel URL and version are in your welcome email; if it
# gets lost, contact support and we will re-send it. The endpoint
# only serves wheels to active licenses.
pip install https://billing.vexinter.com/pro/vexinter_pro-<version>-py3-none-any.whl
```

The endpoint only serves the wheel while the license is `active` or
`past_due`, cancelled or expired subscriptions get HTTP 403/401.

---

## Quickstart

### Local runtime (no service)

```python
from vexinter.sdk import Task
from vexinter.core.runtime import LocalRuntime
from vexinter.demo import CrmAdapter, BillingAdapter

rt = LocalRuntime([CrmAdapter(), BillingAdapter()])

with Task(rt, agent="support_bot", objective="Create customer and invoice",
          condition={"op": "equals",
                     "a": {"from": "crm", "path": "status"},
                     "b": "active"},
          input={"email": "customer@example.com"}) as t:
    c = t.tools.crm.create_customer(
        {"name": "Jane Doe", "email": "customer@example.com"})
    t.tools.billing.create_invoice({"customer_id": c.id, "amount": 150})
    verdict = t.verify()
    print(verdict.status)
```

### Remote service (HTTP)

Linux/macOS (bash):

```bash
# 0. Clone, configure and start the service
git clone https://github.com/Vexinter-AI/vexinter && cd vexinter
cp .env.example .env   # set VEXINTER_ADMIN_TOKEN to a strong secret
export VEXINTER_ADMIN_TOKEN=<the same value you put in .env>
docker compose up --build -d   # starts PostgreSQL + API in the background

# 1. Enroll an agent (admin token)
curl -X POST http://localhost:8000/v1/identity/agents \
     -H "X-Vex-Admin: $VEXINTER_ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"agent_id": "bot-1", "allowed_tools": ["crm.create_customer"]}'

# 2. Spawn an agent instance (admin token)
curl -X POST http://localhost:8000/v1/identity/instances \
     -H "X-Vex-Admin: $VEXINTER_ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"agent_id": "bot-1"}'

# 3. Issue a credential for the instance (admin token). `inst_…` is the
#    `instance_id` field returned by step 2. First generate the agent's
#    key material — Vexinter only ever stores the PUBLIC halves; the
#    private halves stay on your machine and sign every request. They
#    are written to a file with 0600 permissions (never printed, so
#    they do not end up in your shell history):
pip install vexinter   # Community edition, if not already installed
python3 - <<'PY'
import json, os
from vexinter.identity import AgentKeyMaterial
k = AgentKeyMaterial()
fd = os.open("bot-1.keys.json",
             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"signing_priv": k.signing_priv_b64,
               "encryption_priv": k.encryption_priv_b64}, f)
print("signing_pub    =", k.signing_pub_b64)
print("encryption_pub =", k.encryption_pub_b64)
PY
#    (if bot-1.keys.json already exists, delete it or pick another name)
#    paste the two *_pub values into the curl below. The response's
#    `credential_id` is the `<agent-credential>` used in step 4:
curl -X POST http://localhost:8000/v1/identity/credentials \
     -H "X-Vex-Admin: $VEXINTER_ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"instance_id": "inst_…", "signing_pub": "…", "encryption_pub": "…"}'

# 4. Create a task. The data plane does not accept Bearer tokens: every
#    request is signed with the agent's Ed25519 private key
#    (X-Vex-Credential/Timestamp/Sequence/Signature headers), so raw
#    curl cannot do it — use the client, which signs every call:
python3 - <<'PY'
from vexinter.identity import AgentKeyMaterial
from vexinter.remote import RemoteCredentials, RemoteRuntime, RemoteTask

import json
_k = json.load(open("bot-1.keys.json"))
keys = AgentKeyMaterial.from_private_b64(
    _k["signing_priv"], _k["encryption_priv"])
creds = RemoteCredentials(
    agent_id="bot-1",
    instance_id="<inst_… from step 2>",
    credential_id="<credential_id from step 3>",
    keys=keys)

with RemoteRuntime("http://localhost:8000", credentials=creds) as rt:
    with RemoteTask(rt, agent="bot-1",
                    objective="Create customer and invoice",
                    condition={"op": "equals",
                               "a": {"from": "crm", "path": "status"},
                               "b": "active"},
                    input={"email": "customer@example.com"},
                    idempotency_key="order-12345") as t:
        print(t.verify().status)
PY
```

The Python client (`vexinter.remote.RemoteTask` / `AsyncRemoteTask`,
sync/async) ships inside the `vexinter` package under the Community
License — it is the same package as the runtime, not a separate SDK.
`@vexinter-ai/sdk` on npm (`AsyncRemoteRuntime`) is MIT licensed. All
three accept `idempotency_key` and a `budget` object.

### Real-world example: invoicing agent on Invoice Ninja

The demo adapters above are in-memory. For a real deployment, Vexinter
ships adapters for **Invoice Ninja** (self-hosted or cloud) in the
**Community edition**, every write goes through the verification
pipeline against the live instance:

```python
import os
from vexinter import Vexinter
from vexinter.invoiceninja import (
    InvoiceNinjaBilling, InvoiceNinjaClient, InvoiceNinjaCrm,
)

client = InvoiceNinjaClient(
    os.environ["INVOICENINJA_URL"], os.environ["INVOICENINJA_API_TOKEN"])

vex = Vexinter(embedded=True,
               adapters=[InvoiceNinjaCrm(client),
                         InvoiceNinjaBilling(client)])

# Onboard a customer and issue their first invoice. The condition is
# evaluated against the REAL instance: not against what the agent
# claims it did.
CONDITION = {
    "op": "all",
    "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "customer"},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "customer.id"}}},
         "equals": 1},
    ],
}

with vex.task(agent="billing_bot",
              objective="Onboard jane@acme.com and invoice her $150",
              condition=CONDITION,
              input={"email": "jane@acme.com"}) as t:
    c = t.tools.crm.create_customer(
        {"name": "Jane", "email": "jane@acme.com"})
    t.tools.billing.create_invoice({"customer_id": c.id, "amount": 150})
    print(t.verify().status)   # met | failed | inconclusive
```

The CRM adapter deduplicates customers by email, a retry after a
network cut returns the existing record instead of creating a
duplicate, and the billing adapter supports a human-approval mode
for invoices above a threshold. Pair it with the HITL endpoints
(`/v1/tasks/{id}/hitl/*`) for a four-eyes check before money moves.

---

## Tasks, quota and guardrails

### Quota semantics

- A task consumes **1 unit of quota when it is accepted**, before the
  first tool call. Failed, aborted and cancelled tasks still count.
- Validation rejections (422), quota rejections (429) and auth failures
  (401/403) are **free**, they never consume quota.
- Subtasks do **not** consume separate quota: they inherit the parent
  budget recursively and are bounded by `VEXINTER_MAX_SUBTASK_DEPTH`
  (default 3) and `VEXINTER_MAX_SUBTASKS_PER_TASK` (default 32).
- Quotas reset on the **calendar month** (UTC, day 1 → last day), per
  subscriber and, on multi-tenant deployments, per tenant.
- The hard block applies **only to new task creation**. Tasks already
  running are never interrupted when the quota is reached, they run
  to completion (or to their own budget/guardrail limits).
- `POST /v1/tasks` responses include a `warnings` array. Once usage
  reaches **80%** of the monthly quota, every new task carries a
  `monthly quota N/M tasks used` warning so operators can upgrade or
  throttle before the block engages. The service also emits a
  `quota_threshold` **audit event** at the 80/90/100% bands (once per
  subscriber per billing period per band), alert on the
  `vexinter.audit` log stream (`event_type=quota_threshold`) instead of
  polling task responses.
- **Self-hosted caveat (known limitation):** on your own deployment
  the counter lives in *your* database. Wiping the database or
  pointing the service at a fresh instance resets the monthly count.
  This is accepted for self-hosted (the license is the legal
  enforcement).

#### Preserving quota state on self-hosted deployments

The counter is only as durable as the database behind it. The default
in-memory event store resets **everything** — executions, quota and
memory — on every restart, so any deployment expected to enforce a
paid plan must run on PostgreSQL (`DATABASE_URL` / `POSTGRES_*`),
where usage lives in the `vex_tier_usage` table.

Common ways teams accidentally reset the monthly count:

- **Ephemeral databases in CI/CD.** Pipelines that recreate the
  Postgres container (or the whole compose stack) on every deploy
  destroy `vex_tier_usage` along with everything else. A junior
  engineer's routine deploy silently gives the customer a fresh
  500/5,000-task month — a commercial-terms violation with no bad
  intent involved.
- **Stateless pods.** On Kubernetes, running Postgres inside the app
  pod (or on an `emptyDir` volume) means every pod reschedule or
  rolling update wipes the counters.
- **Restores and migrations.** Rebuilding the database from an old
  backup rolls `tasks_used` back to whatever it was at backup time.

Recommended architecture for commercial self-hosted installs:

- **Dedicated persistent volume for the database.** Postgres data
  gets its own volume, separate from application containers, so
  application redeploys never touch it.
- **Kubernetes:** run Postgres as a `StatefulSet` (or a managed
  instance) with a `PersistentVolumeClaim` using a `Retain` reclaim
  policy. Never mount the database on the same PVC/lifecycle as the
  Vexinter pods — pod churn must not be able to destroy the volume.
  Exclude the database workload from the ephemeral CI pipeline.
- **Backups that include `vex_tier_usage`.** If you restore, restore
  usage too — a database rebuilt "clean" is a quota reset. For audit
  purposes, periodically export the usage table alongside your
  backups.
- **The license is the enforcement layer; the counter is operational
  telemetry.** If your threat model includes a customer
  intentionally wiping their own database to reset quota, that is a
  contract matter (the Terms prohibit circumventing plan limits),
  not something the software can prevent on infrastructure you do
  not control.

### Idempotency

`POST /v1/tasks` accepts `idempotency_key`. Retrying after a network
cut returns the **same task** with `deduplicated: true` and does not
double-consume quota. Reusing a key with a different payload returns
`409`. Keys are deduplicated through the event store, so they survive
restarts.

Scope note: idempotency deduplicates *task creation* and adapter-level
retries. It does not prevent an agent from issuing two distinct write
calls, verification detects the duplicate effect (e.g. oracle record
count > 1), fails the condition and escalates; nothing is silently
merged or dropped.

### Budgets

Each task accepts an optional `budget`:

```json
{
  "max_tokens": 50000,
  "max_usd": 0.50,
  "max_steps": 40,
  "deadline_s": 600,
  "loop_threshold": 3,
  "cycle_threshold": 3,
  "no_progress_window": 8
}
```

- `max_tokens` is the **reliable** ceiling, `max_usd` depends on the
  client-supplied `model_pricing` table and is only as accurate as that
  table.
- Community tasks are hard-capped at `VEXINTER_COMMUNITY_MAX_TOKENS`
  (default 50,000) regardless of what the client asks for.
- Creating a task without a budget logs a warning; explicit budgets are
  recommended for production.

### Execution guardrails

The budget tracker enforces, per execution (invoke and MCP paths):

| Guardrail | Default | Env override |
| --- | --- | --- |
| Max tool calls per execution | 100 | `VEXINTER_MAX_STEPS` or `budget.max_steps` |
| Wall-clock deadline | 1800 s | `VEXINTER_TASK_DEADLINE_S` or `budget.deadline_s` |
| Identical consecutive calls | 3 | `VEXINTER_LOOP_THRESHOLD` or `budget.loop_threshold` |
| Repeating cycles (period 2–8) | 3 repetitions | `VEXINTER_CYCLE_THRESHOLD` or `budget.cycle_threshold` |
| No-progress window | 8 calls | `VEXINTER_NO_PROGRESS_WINDOW` or `budget.no_progress_window` |

A "no-progress" violation means a sliding window of N calls produced
**no new outcome** (tool + args + result). Repeated calls that return
different data, legitimate polling, count as progress.

Violations abort the execution with an `EXECUTION_LIMIT` audit event;
the task can then be recovered or completed manually.

---

## SDKs

### Python: sync and async

```python
from vexinter.remote import RemoteRuntime, RemoteTask

with RemoteRuntime("http://localhost:8000", credentials=creds) as rt:
    with RemoteTask(rt, agent="bot-1", objective="...",
                    condition={...},
                    idempotency_key="order-12345",
                    budget={"max_tokens": 50000}) as t:
        t.tools.crm.create_customer({...})
        print(t.verify().status)
```

`AsyncRemoteTask`/`AsyncRemoteRuntime` mirror the same API with
`await`. Both accept `idempotency_key` and `budget` (a `Budget`
instance or a dict with the fields above).

### TypeScript

```bash
npm install @vexinter-ai/sdk
```

```ts
import { AsyncRemoteRuntime, createAgentKeyMaterial } from '@vexinter-ai/sdk'

// Admin plane: enroll agent, spawn instance, issue credential
const adm = new AsyncRemoteRuntime({
  endpoint: 'http://localhost:8000',
  adminToken: process.env.VEXINTER_ADMIN_TOKEN!,
})
await adm.enrollAgent('bot-ts', { allowedTools: ['crm.create_customer'] })
const inst = await adm.spawnInstance('bot-ts')
const creds = await adm.issueCredential(inst.instance_id, createAgentKeyMaterial())

// Data plane
const rt = new AsyncRemoteRuntime({ endpoint: 'http://localhost:8000', credentials: creds })
const task = await rt.task({
  agent: 'bot-ts', objective: 'Create customer', condition: {...},
  idempotencyKey: 'order-12345',
  budget: { max_tokens: 50000 },
}).enter()
console.log((await task.verify()).status)
```

The wire envelopes are identical in both SDKs, interoperability is
tested in both directions.

---

## HTTP API overview

Base URL: your deployment URL — `http://localhost:8000` for the
`docker compose` quickstart above, or your own domain in production.
Interactive reference: `GET /openapi.json`.

**Errors and rate limits:** errors return an HTTP status with a JSON
body (`{"detail": "..."}`). Status codes in use: `400` malformed
request, `401` missing/invalid credential or signature, `403`
forbidden (admin required, suspended tenant, license instance cap),
`404` unknown resource, `409` idempotency-key payload conflict,
`422` validation failure, `429` rate limit or quota exhaustion.

`429` covers two cases: per-client rate limiting
(`VEXINTER_RATE_LIMIT_RPS`, default 100; `VEXINTER_RATE_LIMIT_BURST`,
default 20 — response includes `retry_after` seconds) and monthly task
quota exhaustion (`detail` carries error `QUOTA_EXCEEDED`; free
rejections never consume quota). Long result sets are paginated
where the endpoint supports it — check `/openapi.json` for each
operation's parameters.

### Authentication

Three credential types, used in different planes:

- **Admin token** (`X-Vex-Admin` header or `vex_spa` HttpOnly cookie
  after `POST /v1/auth/login`): operator console, admin endpoints,
  `/metrics`. This token grants full control of the deployment: keep it
  server-side only, rotate it on a schedule, and never ship it in
  client-side code — the TypeScript `adminToken` option is for trusted
  backend scripts, not browser bundles. Off localhost it must only travel over
  TLS.
- **Agent credential**: data plane — task creation, tool invocation,
  verification. Credentials are issued per agent instance and carry
  the agent's allowed-tool policy. There is no bearer token: every
  request is signed with the agent's Ed25519 private key via the
  `X-Vex-Credential`, `X-Vex-Timestamp`, `X-Vex-Sequence` and
  `X-Vex-Signature` headers; the SDKs do this automatically.
- **Tenant binding**: agents can be bound to a tenant, in which case
  they authenticate with the same signed credential and inherit the
  tenant's quota bucket and suspension status.

### Core endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /v1/tasks` | Create a task (`idempotency_key`, `budget`, `condition`, `input`) |
| `GET /v1/tasks/{id}` | Task state and verdict |
| `POST /v1/tasks/{id}/invoke` | Invoke a declared tool inside the task |
| `POST /v1/tasks/{id}/verify` | Evaluate the condition → verdict |
| `POST /v1/tasks/{id}/complete` | Close the task |
| `POST /v1/tasks/{id}/recover` | Recover an aborted/failed task |
| `GET /v1/tasks/{id}/events` | Event stream for the task |
| `GET /v1/tasks/{id}/audit` | Audit-chain verification |
| `POST /v1/tasks/{id}/subtasks` | Delegate a bounded subtask |
| `GET /v1/tasks/{id}/subtasks` | List subtasks |
| `POST /v1/tasks/{id}/mcp/invoke` | Invoke an MCP-proxied tool |
| `POST /v1/tasks/{id}/delegation/{sequential,parallel}` | Multi-agent delegation |
| `GET/POST /v1/tasks/{id}/messages` | Agent-to-agent signed messages |
| `POST /v1/cases/{id}/decide` | Human-in-the-loop decision |
| `GET /v1/tasks/{id}/hitl/pending` | Pending HITL cases |
| `POST /v1/privacy/erase` | GDPR erasure for an execution |
| `GET /v1/gdpr/erasure-requests` | Erasure request status (admin) |
| `GET /v1/memory`, `/v1/memory/search` | Cross-session memory |
| `GET /v1/events` | Global event query (admin) |
| `GET /metrics` | Prometheus metrics (admin) |
| `GET /health`, `/ready`, `/live` | Probes |

Admin plane (all under `X-Vex-Admin` or the SPA cookie):
`/v1/admin/summary`, `/v1/admin/tasks/{id}`, `/v1/admin/tenants*`
(CRUD, suspend/activate, members, agent binding), `/v1/identity/*`
(agent enrollment, instances, credentials, rotation), `/v1/cases*`,
`/v1/chains/verify`, `/v1/metrics`.

---

## Configuration

Copy `.env.example` to `.env`. The essentials:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` / `POSTGRES_*` | PostgreSQL event store (required in production) |
| `VEXINTER_ADMIN_TOKEN` | Operator token for the admin plane |
| `VEXINTER_DEV_MODE` | `1` honors `VEXINTER_DEFAULT_PLAN`; unset in prod |
| `VEXINTER_DEFAULT_SUBSCRIBER_EMAIL` | Subscriber identity for quota bucketing |
| `VEXINTER_MAX_STEPS` | Default per-execution tool-call cap (100) |
| `VEXINTER_TASK_DEADLINE_S` | Default per-execution deadline (1800) |
| `VEXINTER_COMMUNITY_MAX_TOKENS` | Community per-task token ceiling (50000) |
| `VEXINTER_MAX_SUBTASK_DEPTH` | Subtask nesting limit (3) |
| `VEXINTER_MAX_SUBTASKS_PER_TASK` | Subtasks per task (32) |
| `VEXINTER_LICENSE_BASE_URL` | License document endpoint (Pro only) |
| `VEXINTER_LICENSE_ID` | Customer id (`lic_…`) the service fetches (Pro only) |
| `VEXINTER_LICENSE_TOKEN` | Secret license/download token (Pro only) |
| `VEXINTER_LICENSE_CACHE` | On-disk license cache path (Pro only) |
| `VEXINTER_LICENSE_TTL_S` | License cache TTL before revalidation (86400) |
| `VEXINTER_LICENSE_MAX_STALE_S` | Max staleness when the endpoint is down (604800) |
| `VEXINTER_DEFAULT_PLAN` | Plan override, honored only with `VEXINTER_DEV_MODE=1` |
| `VEXINTER_RATE_LIMIT_RPS` / `VEXINTER_RATE_LIMIT_BURST` | Per-client API rate limit (100 / 20) |
| `VEXINTER_NOTIFY_WEBHOOK_URL` | Outbound notifications webhook (Slack-compatible) |
| `VEXINTER_NOTIFY_EVENTS` | Comma-separated event types to notify (default: all noteworthy) |

`VEXINTER_LICENSE_PUBKEY`/`VEXINTER_LICENSE_PUBKEYS` are deprecated and
ignored, the verification root key is embedded in the package.

License variables are read by `vexinter-pro`; the Community core
ignores them.

---

## Plans and licensing

- **Community**, free, source-available under the Vexinter Community
  License (non-commercial; converts to MIT two years after each
  release). 500 tasks/month.
- **Professional / Business**, commercial plans under the Terms of
  Service. Enforcement requires both a verified signed license
  document **and** the `vexinter-pro` package; a paid plan string
  without them resolves to Community.
- License documents are Ed25519-signed (v2: payload signed by a
  document key that is itself attested by the vendor **root key
  compiled into the wheel**, so paid plans cannot be enabled by
  configuration or client-side changes alone). The runtime verifies
  the signature automatically on every fetch — you never handle keys.
  Wheel downloads are served only over TLS from the authenticated
  package endpoint while the license is active. License documents
  are fetched from `VEXINTER_LICENSE_BASE_URL` with the
  `VEXINTER_LICENSE_TOKEN` Bearer credential (license endpoint only; the customer id alone is
  not sufficient), cached
  for `VEXINTER_LICENSE_TTL_S` (default 24 h) and accepted stale for
  at most `VEXINTER_LICENSE_MAX_STALE_S` (default 7 days) if the
  endpoint is unreachable. Cancelled/expired docs always resolve to
  Community.
- Paid plans are also bound to infrastructure: the signed document
  carries `max_instances` (Professional: 5 concurrent agent instances,
  Business: unlimited). Exceeding it rejects `POST
  /v1/identity/instances` with 403, one seat cannot stretch across an
  unbounded fleet.

### License lifecycle and degradation

**Resolution timeline.** The license document is cached for
`VEXINTER_LICENSE_TTL_S` (24 h). If the license endpoint is
unreachable, the cached document is accepted stale for up to
`VEXINTER_LICENSE_MAX_STALE_S` (7 days) — a network outage does not
degrade a paying customer. A **cancelled or expired** document
resolves to Community immediately; there is no additional grace
period beyond the stale-cache window.

**What changes when a plan degrades to Community.** Degradation is a
*limit* change, not a shutdown — Vexinter never destroys customer
infrastructure:

- **Running work completes.** The quota hard block applies only to
  *new* task creation (`POST /v1/tasks`). Tasks already executing
  run to completion or to their own budget/guardrail limits. Nothing
  is killed mid-flight.
- **New tasks are rejected.** Once the Community quota (500
  tasks/month) is reached, `POST /v1/tasks` returns **403** with
  `detail: "Tier community limit reached: N/500 tasks this month.
  Upgrade to continue."` The rejection is free — it does not consume
  quota — and idempotent replays of already-accepted tasks still
  return their original result.
- **Instances are not revoked.** "Agent instances" are identity
  enrollments, not containers — Vexinter does not manage or destroy
  customer containers at all. Enrolled agents and issued credentials
  keep working after a downgrade; only *spawning new* instances is
  capped, and only for paid plans (Professional: 5). Community has
  **no instance cap**, so existing fleets are unaffected. Credentials
  are revoked only by explicit admin action
  (`DELETE /v1/identity/credentials/{id}`), never automatically.
- **Plan-gated features resolve to Community.** Anything licensed to
  paid tiers (larger quotas, higher token ceilings, the Pro package
  features) falls back to Community limits on the next license
  resolution — within the 24 h cache TTL at most.

**Client-side handling.** A well-behaved integration should treat the
quota denial as a circuit breaker, not retry it — retrying a 403
quota rejection just burns requests:

```ts
// Quota circuit breaker for POST /v1/tasks.
// The service returns 403 { detail: "Tier <plan> limit reached: ..." }
// when the monthly quota is exhausted. Open the circuit on that
// denial and half-open it when usage shows headroom again (the
// quota resets on the calendar month, UTC).

interface Usage {
  tasks_used: number;
  task_limit: number;
  period_end: string;
}

function isQuotaDenial(err: unknown): boolean {
  const e = err as { status?: number; body?: { detail?: string } };
  return e?.status === 403 &&
    /limit reached/.test(e?.body?.detail ?? "");
}

class QuotaGate {
  private blocked = false;

  constructor(
    private createTask: () => Promise<unknown>,
    private getUsage: () => Promise<Usage>,
  ) {}

  async submit(): Promise<unknown> {
    if (this.blocked) {
      const u = await this.getUsage();
      if (u.tasks_used < u.task_limit) this.blocked = false;
      else {
        throw new Error(
          `monthly task quota exhausted (${u.tasks_used}/${u.task_limit}), ` +
          `resets ${u.period_end}`);
      }
    }
    try {
      return await this.createTask();
    } catch (err) {
      if (isQuotaDenial(err)) this.blocked = true;
      throw err;
    }
  }
}
```

`GET /v1/billing/usage` (admin auth) returns `tasks_used`,
`task_limit`, `hard_block` and `period_end` — the same counters the
enforcement path uses — so the gate can poll it instead of guessing
when the month rolled over. Schedule paused work for `period_end`;
do not retry in a tight loop, the block lasts until the calendar
month resets or the plan is upgraded.

See `TERMS-OF-SERVICE.md` for the full plan table and the
`LICENSE-COMMUNITY.md` file for the community license text.

---

## Observability and audit

- `GET /metrics`, Prometheus exposition (admin auth).
- `GET /openapi.json`, full OpenAPI schema.
- Every state transition lands on a hash-chained event store;
  `GET /v1/tasks/{id}/audit` verifies the chain for a task.
- Quota consumption, tier overages (none, hard block), guardrail
  violations and license state changes are audit events.

## Notifications

Set `VEXINTER_NOTIFY_WEBHOOK_URL` to receive outbound notifications for
noteworthy store events, executions completing, verifier verdicts,
policy/tool rejections, recovery decisions, human-approval requests,
privacy erasures, guardrail trips, etc.

The dispatcher POSTs a JSON body with both Slack-compatible `text` and
Discord-compatible `content` fields plus structured fields
(`event_type`, `summary`, `task_id`, `execution_id`, `agent_id`,
`data`), so Slack and Discord incoming-webhook URLs work as-is and
generic receivers can consume the structured payload.
Delivery is asynchronous and best-effort: a bounded queue feeds the
channels, full queues drop and count notifications, and channel
failures are logged, they never block or lose store events.

`VEXINTER_NOTIFY_EVENTS` optionally restricts which event types are
emitted (comma-separated `EventType` values, e.g.
`execution.completed,verifier.verdict,tool.rejected`).

**Slack**: create an app at api.slack.com → *Incoming Webhooks* →
*Activate Incoming Webhooks* → *Add New Webhook to Workspace* → pick a
channel → copy the `https://hooks.slack.com/services/...` URL.

**Discord**: server → *Channel Settings* → *Integrations* → *Webhooks*
→ *New Webhook* → *Copy Webhook URL*.

**Generic receivers**: any HTTP endpoint accepting a JSON POST, the
structured fields carry the full event context.

### Quota alerting

The service emits a `quota_threshold` **audit event** once per
subscriber per billing period at each of the 80%, 90% and 100% bands
(`event_type=quota_threshold`, `data.band` = 80/90/100). It is not a
store event, so it does not flow through the notification webhook —
alert on the audit log stream instead. A typical pipeline is to ship
`vexinter.audit` to your log aggregator and create an alert rule on
`event_type=quota_threshold`:

- **Band 80/90** → informational: notify the channel that a customer
  is approaching the block.
- **Band 100 mid-period** → anomaly worth a human look. If a
  subscriber hits 100% in the first days of the month, check whether
  usage is legitimately concentrated (a batch job, a runaway agent
  loop) or whether the counter was reset by a database rebuild —
  see "Preserving quota state" above.
- **Counter resets** → also alert on `tasks_used` *decreasing*
  within the same `period_start` (the `task_recorded` audit events
  show the running total). A drop means the usage table was wiped or
  the service was repointed at a fresh database.

Slack/Discord alerting on these events is just your log pipeline's
standard webhook integration — no Vexinter-side configuration beyond
enabling audit logging is required.

## Verification caveats

Verification is only as good as the condition you write:

- **Conditions are authored by people.** If a condition is incomplete or
  mis-specified, the verifier faithfully reports `met`, a green verdict
  inherits the author's blind spots. There is no formal guarantee that a
  declared condition captures what you actually meant.
- **A verdict is evidence, not absolution.** `met` means "the declared
  condition held against the recorded state", it does not certify that
  the agent behaved well, that the condition was the right one, or that
  the underlying system is trustworthy. Design conditions accordingly.
- **Cost and latency are real.** Every verification re-reads state and
  evaluates a condition; agents that need per-action certainty pay that
  cost on every step. Batch or sample verification where the risk
  allows.

## Memory

Cross-session memory is built in (`GET /v1/memory`,
`/v1/memory/search`) and organized in three scopes:

- **Episodic** — concrete events with timestamps.
- **Semantic** — general facts and knowledge.
- **Working** — active task context.

Entries carry embedding vectors for semantic search, a temporal decay
factor, and an expiry model with periodic re-verification. Scoped
access, GDPR erasure (`POST /v1/privacy/erase`) and audit coverage are
part of the same store, memory is evidence, not a black box.

**Persistence:** the default event store is in-memory for development;
production deployments persist to PostgreSQL via `DATABASE_URL` /
`POSTGRES_*`. Sensitive fields can be redacted at rest through the
privacy layer; encrypt your database volume if your threat model
requires it — Vexinter does not manage disk-level encryption (see
below).

### Data at rest and key management

Vexinter encrypts personal fields at the application layer
(`PrivacyStore`, AES-256-GCM, one key per execution) and messages
end-to-end, but the **database volume itself is not encrypted by the
service**. If the Postgres volume, a snapshot, or a `pg_dump` backup
leaks, an attacker gets event metadata, usage counters and license
caches in cleartext (personal fields stay ciphertext). Treat disk
encryption as your responsibility — concrete options:

- **Bare-metal / own Linux server:** LUKS on the volume that hosts
  the Postgres data directory. `cryptsetup` on the data partition,
  mount it, point `PGDATA`/`DATABASE_URL` at the unlocked mount, and
  add the volume to your boot/unlock runbook. Full-disk encryption
  (LUKS over LVM) is the stronger variant if the whole host is
  dedicated to Vexinter.
- **Cloud VM:** enable provider volume encryption (encrypted EBS /
  Persistent Disk / managed disk) and manage the volume key through
  the cloud KMS — AWS KMS, Azure Key Vault or GCP Cloud KMS — so key
  material is not stored next to the data it protects. Apply the
  same to snapshots and backups: an unencrypted `pg_dump` defeats
  volume encryption.
- **Managed Postgres:** most providers encrypt storage at rest by
  default and offer customer-managed keys (CMK) — enable CMK if your
  compliance posture requires it.

Keep the secrets that *do* unlock application-level ciphertext out of
the database itself: `VEXINTER_LICENSE_TOKEN`, database credentials,
and any master keys belong in your secret manager or KMS, not in
tables the service can read.

#### Key rotation workflow

If a long-term private key is compromised, every message ever
encrypted to the matching public key becomes decryptable — rotation
limits the exposure window but cannot undo past disclosures. The
rotation is **agent-initiated** (private keys never leave the agent
host), and the previous credential stays valid for a grace period so
in-flight work is not interrupted:

1. **Detect.** Audit log `credential.*` events and unexpected
   `401/403` signature failures on the data plane.
2. **Rotate.** The agent calls `POST /v1/identity/credentials/rotate`
   (SDK: `RemoteRuntime.rotate_credentials()` /
   `runtime.rotateCredentials()`), which derives successor keys
   locally, registers the new public halves and returns a new
   `credential_id`. Persist the new private material immediately —
   the old credential stops working at the end of the grace period.
3. **Revoke if needed.** If the key may already be abused, skip the
   grace period: admin `DELETE /v1/identity/credentials/{id}` kills
   the credential immediately, then issue a fresh one via
   `POST /v1/identity/credentials`.
4. **Audit.** Rotation is a data-plane operation (the agent signs
   with its current credential); revocation is admin-plane
   (`require_admin`). Both are recorded as
   `identity.credential_issued` / `identity.credential_revoked`
   events in the hash-chained event store — verify the chain if the
   incident is disputed.

Automate step 2 with a scheduled job on the agent host. Example —
weekly rotation via cron:

```python
#!/usr/bin/env python3
# /opt/vexinter/rotate_keys.py — weekly credential rotation.
# cron:  17 4 * * 1 /opt/vexinter/venv/bin/python /opt/vexinter/rotate_keys.py
import json, os
from vexinter.identity import AgentKeyMaterial
from vexinter.remote import RemoteCredentials, RemoteRuntime

KEYS = os.path.expanduser("~/.vexinter/bot-1.keys.json")
k = json.load(open(KEYS))
keys = AgentKeyMaterial.from_private_b64(
    k["signing_priv"], k["encryption_priv"],
    previous=k.get("previous_keys"))
creds = RemoteCredentials(
    agent_id=k["agent_id"], instance_id=k["instance_id"],
    credential_id=k["credential_id"], keys=keys)

with RemoteRuntime(os.environ["VEXINTER_ENDPOINT"],
                   credentials=creds) as rt:
    new = rt.rotate_credentials()          # old cred valid ~15 min grace

# Persist the successor material atomically; the old file is the
# rollback path until the grace period ends.
tmp = KEYS + ".tmp"
with open(tmp, "w") as f:
    json.dump({
        "agent_id": new.agent_id,
        "instance_id": new.instance_id,
        "credential_id": new.credential_id,
        "signing_priv": new.keys.signing_priv_b64,
        "encryption_priv": new.keys.encryption_priv_b64,
        "previous_keys": new.keys.previous_keys_serialized(),
    }, f)
os.chmod(tmp, 0o600)
os.replace(tmp, KEYS)
```

Run it no more often than the grace window makes useful (weekly is
plenty) and monitor for rotation failures — a silently failing cron
means your credentials are aging. On the admin side, periodically
list credentials (`GET /v1/identity/credentials`) and revoke any you
cannot account for.

## Secure agent-to-agent messaging

Agent messages travel as signed, end-to-end encrypted envelopes over
`GET/POST /v1/tasks/{id}/messages`:

- **Encryption**: ephemeral X25519 ECDH per message → HKDF → AES-256-GCM.
  The service relays ciphertext; it cannot read message bodies.
- **Authentication**: Ed25519 signatures over the envelope, verified
  against the sender's registered public keys, with validity-window and
  replay/sequence checks.
- **Rotation**: key material rotates with a grace window so in-flight
  messages still verify; rotation is an admin-plane operation
  (`/v1/identity/*`).
- **Threat model**: encryption is ephemeral-static ECDH — message
  bodies have forward secrecy against ephemeral-key disclosure, but
  **not** against compromise of the recipient's long-term private key
  (past messages to that recipient become decryptable). The relay sees
  metadata — sender, recipient, timestamps and sizes — but never
  content. Protect the admin plane and key storage accordingly.

## Versioning and compatibility

The project is currently in **0.x**: breaking changes can appear in
minor releases, so pin your version and read the changelog before
upgrading. Patch releases are bug fixes; wire envelopes and persisted
schemas carry explicit versions; deprecated variables and endpoints
are marked in `.env.example` and `/openapi.json` before removal. The
changelog is the GitHub Releases page of the repository.

## Support

- Security issues: see `SECURITY.md`.
- Questions: `support@vexinter.com`.
