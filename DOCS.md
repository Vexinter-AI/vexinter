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

## Editions

Vexinter is open-core:

| Edition | Source | Plans |
| --- | --- | --- |
| **Community** | Public repo (`Vexinter-AI/vexinter`) | Community tier: 500 tasks/month, hard block |
| **Pro** | Private package `vexinter-pro` | Professional (5,000 tasks/month) and Business (50,000 tasks/month), both hard-blocked at the limit |

The public core contains only the Community tier and a registration
hook. Paid tiers and the signed-license client ship in `vexinter-pro`,
which registers itself at import time — the paid **features** and the
**license validation** code are simply not present in the public
repository, so a paid plan cannot be activated from the public tree
alone. Note that the Community task cap itself is a constant in the
public source: like any source-available project, the license — not
the code — is the legal enforcement against raising it.

### Installing the Pro package

Subscribers receive a customer id (`lic_…`, a **public identifier**)
and a secret **license token** in the welcome email. Two different
things, two different channels:

- The **token** authenticates every request to the license and
  package endpoints — it travels in headers (`.netrc`/`Authorization`),
  never in URLs, so it does not end up in shell history, CI logs or
  proxy logs.
- The **customer id** only identifies *which* license to check; it is
  not a credential.

License enforcement — add to `.env`:

```bash
VEXINTER_LICENSE_BASE_URL=https://billing.vexinter.com/license
VEXINTER_LICENSE_ID=lic_xxxxxxxxxxxxxxxx
VEXINTER_LICENSE_TOKEN=<token from the welcome email>
VEXINTER_LICENSE_PUBKEY=<public key from the welcome email>
```

Wheel download — add to `~/.netrc` (`chmod 600`) so pip authenticates
without the token touching your shell history:

```
machine billing.vexinter.com login lic_xxxxxxxxxxxxxxxx password <token>
```

```bash
pip install https://billing.vexinter.com/pro/vexinter_pro-0.2.0-py3-none-any.whl
```

The endpoint only serves the wheel while the license is `active`,
`trial` or `past_due` — cancelled or expired subscriptions get HTTP
403/401.

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

```bash
# 1. Start the service (requires PostgreSQL + admin token)
docker compose up --build

# 2. Enroll an agent and issue credentials (admin token)
curl -X POST http://localhost:8000/v1/identity/agents \
     -H "X-Vex-Admin: $VEXINTER_ADMIN_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"agent_id": "bot-1", "allowed_tools": ["crm.create_customer"]}'

# 3. Create a task
curl -X POST http://localhost:8000/v1/tasks \
     -H "Authorization: Bearer <agent-credential>" \
     -H 'Content-Type: application/json' \
     -d '{
       "agent": "bot-1",
       "objective": "Create customer and invoice",
       "condition": {"op": "equals", "a": {"from": "crm", "path": "status"}, "b": "active"},
       "input": {"email": "customer@example.com"},
       "idempotency_key": "order-12345"
     }'
```

The Python SDKs wrap this flow: `vexinter.remote.RemoteTask` /
`AsyncRemoteTask` (sync/async) and `@vexinter-ai/sdk` on npm
(`AsyncRemoteRuntime`). All three accept `idempotency_key` and a
`budget` object.

### Real-world example: invoicing agent on Invoice Ninja

The demo adapters above are in-memory. For a real deployment, Vexinter
ships adapters for **Invoice Ninja** (self-hosted or cloud) — every
write goes through the verification pipeline against the live
instance:

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
# evaluated against the REAL instance — not against what the agent
# claims it did.
CONDICION = {
    "op": "all",
    "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "cliente"},
        {"op": "exists", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "cliente.id"}}},
         "as": "factura"},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "cliente.id"}}},
         "equals": 1},
    ],
}

with vex.task(agent="billing_bot",
              objective="Onboard jane@acme.com and invoice her $150",
              condition=CONDICION,
              input={"email": "jane@acme.com"}) as t:
    c = t.tools.crm.create_customer(
        {"name": "Jane", "email": "jane@acme.com"})
    t.tools.billing.create_invoice({"customer_id": c.id, "amount": 150})
    print(t.verify().status)   # met | failed | inconclusive
```

The CRM adapter deduplicates customers by email — a retry after a
network cut returns the existing record instead of creating a
duplicate — and the billing adapter supports a human-approval mode
for invoices above a threshold. Pair it with the HITL endpoints
(`/v1/tasks/{id}/hitl/*`) for a four-eyes check before money moves.

---

## Tasks, quota and guardrails

### Quota semantics

- A task consumes **1 unit of quota when it is accepted** — before the
  first tool call. Failed, aborted and cancelled tasks still count.
- Validation rejections (422), quota rejections (429) and auth failures
  (401/403) are **free** — they never consume quota.
- Subtasks do **not** consume separate quota: they inherit the parent
  budget recursively and are bounded by `VEXINTER_MAX_SUBTASK_DEPTH`
  (default 3) and `VEXINTER_MAX_SUBTASKS_PER_TASK` (default 32).
- Quotas reset on the **calendar month** (UTC, day 1 → last day), per
  subscriber and — on multi-tenant deployments — per tenant.
- The hard block applies **only to new task creation**. Tasks already
  running are never interrupted when the quota is reached — they run
  to completion (or to their own budget/guardrail limits).
- `POST /v1/tasks` responses include a `warnings` array. Once usage
  reaches **80%** of the monthly quota, every new task carries a
  `monthly quota N/M tasks used` warning so operators can upgrade or
  throttle before the block engages.
- **Self-hosted caveat (known limitation):** on your own deployment
  the counter lives in *your* database. Wiping the database or
  pointing the service at a fresh instance resets the monthly count.
  This is accepted for self-hosted (the license is the legal
  enforcement); the hosted service at `api.vexinter.com` enforces
  quotas server-side and cannot be reset by the client.

### Idempotency

`POST /v1/tasks` accepts `idempotency_key`. Retrying after a network
cut returns the **same task** with `deduplicated: true` and does not
double-consume quota. Reusing a key with a different payload returns
`409`. Keys are deduplicated through the event store, so they survive
restarts.

### Budgets

Each task accepts an optional `budget`:

```json
{
  "max_tokens": 50000,
  "max_usd": 0.50,
  "max_steps": 40,
  "deadline_s": 600,
  "loop_threshold": 5,
  "cycle_threshold": 3,
  "no_progress_window": 8
}
```

- `max_tokens` is the **reliable** ceiling — `max_usd` depends on the
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
| Identical consecutive calls | 5 | `budget.loop_threshold` |
| Repeating cycles (period 2–8) | 3 repetitions | `budget.cycle_threshold` |
| No-progress window | 8 calls | `budget.no_progress_window` |

A "no-progress" violation means a sliding window of N calls produced
**no new outcome** (tool + args + result). Repeated calls that return
different data — legitimate polling — count as progress.

Violations abort the execution with an `EXECUTION_LIMIT` audit event;
the task can then be recovered or completed manually.

---

## SDKs

### Python — sync and async

```python
from vexinter.remote import RemoteRuntime, RemoteTask

with RemoteRuntime("https://api.vexinter.com", credentials=creds) as rt:
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
  endpoint: 'https://api.vexinter.com',
  adminToken: process.env.VEXINTER_ADMIN_TOKEN!,
})
await adm.enrollAgent('bot-ts', { allowedTools: ['crm.create_customer'] })
const inst = await adm.spawnInstance('bot-ts')
const creds = await adm.issueCredential(inst.instance_id, createAgentKeyMaterial())

// Data plane
const rt = new AsyncRemoteRuntime({ endpoint: 'https://api.vexinter.com', credentials: creds })
const task = await rt.task({
  agent: 'bot-ts', objective: 'Create customer', condition: {...},
  idempotencyKey: 'order-12345',
  budget: { max_tokens: 50000 },
}).enter()
console.log((await task.verify()).status)
```

The wire envelopes are identical in both SDKs — interoperability is
tested in both directions.

---

## HTTP API overview

Base URL: `https://api.vexinter.com` (hosted) or your own deployment.
Interactive reference: `GET /openapi.json`.

### Authentication

Three credential types, used in different planes:

- **Admin token** (`X-Vex-Admin` header or `vex_spa` HttpOnly cookie
  after `POST /v1/auth/login`): operator console, admin endpoints,
  `/metrics`.
- **Agent credential** (`Authorization: Bearer`): data plane — task
  creation, tool invocation, verification. Credentials are issued per
  agent instance and carry the agent's allowed-tool policy.
- **Tenant member credentials**: same bearer scheme, scoped to a
  tenant's quota bucket.

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
| `VEXINTER_LICENSE_PUBKEY` | Ed25519 public key that verifies license docs (Pro only) |
| `VEXINTER_LICENSE_PUBKEYS` | Extra keys accepted during rotation (Pro only) |
| `VEXINTER_LICENSE_CACHE` | On-disk license cache path (Pro only) |
| `VEXINTER_LICENSE_MAX_STALE_S` | Max staleness when the endpoint is down (604800) |

License variables are read by `vexinter-pro`; the Community core
ignores them.

---

## Plans and licensing

- **Community** — free, source-available under the Vexinter Community
  License (non-commercial; converts to MIT two years after each
  release). 500 tasks/month.
- **Professional / Business** — commercial plans under the Terms of
  Service. Enforcement requires both a verified signed license
  document **and** the `vexinter-pro` package; a paid plan string
  without them resolves to Community.
- License documents are Ed25519-signed, fetched from
  `VEXINTER_LICENSE_BASE_URL` with the `VEXINTER_LICENSE_TOKEN`
  Bearer credential (the customer id alone is not sufficient), cached
  for `VEXINTER_LICENSE_TTL_S` (default 24 h) and accepted stale for
  at most `VEXINTER_LICENSE_MAX_STALE_S` (default 7 days) if the
  endpoint is unreachable. Cancelled/expired docs always resolve to
  Community.
- Paid plans are also bound to infrastructure: the signed document
  carries `max_instances` (Professional: 5 concurrent agent instances,
  Business: unlimited). Exceeding it rejects `POST
  /v1/identity/instances` with 403 — one seat cannot stretch across an
  unbounded fleet.

See `TERMS-OF-SERVICE.md` for the full plan table and the
`LICENSE-COMMUNITY.md` file for the community license text.

---

## Observability and audit

- `GET /metrics` — Prometheus exposition (admin auth).
- `GET /openapi.json` — full OpenAPI schema.
- Every state transition lands on a hash-chained event store;
  `GET /v1/tasks/{id}/audit` verifies the chain for a task.
- Quota consumption, tier overages (none — hard block), guardrail
  violations and license state changes are audit events.

## Support

- Security issues: see `SECURITY.md`.
- Questions: `support@vexinter.com`.
