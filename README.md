# Vexinter

**Verification, memory, and secure communication infrastructure for AI agents.**

Vexinter sits between your AI agents and the real world, providing three
fundamental layers that ensure every decision is verifiable, every memory is
useful, and every message is private:

| Layer                        | Function                                                                 | Example                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| **Operational Verification** | Declarative policies, per-action attestation, immutable log              | A refund agent can only execute if `amount <= 500` and the customer is verified          |
| **Dynamic Memory**           | Episodic/semantic/working memory, controlled retention, tenant isolation | Historical context that evolves with the agent and complies with GDPR (right to erasure) |
| **Secure Communication**     | E2E channels between agents, verifiable identity, granular permissions   | Agent A sends an order to Agent B with non-repudiation and tamper-proofing guarantees    |

---

## Features

- **Verifiable execution**: tasks run under declarative conditions; every tool
  call is attested and appended to a hash-chained, append-only audit log.
- **Memory with governance**: episodic/semantic/working memory tiers,
  retention policies, per-person GDPR erasure that preserves chain integrity.
- **Cryptographic identity**: Ed25519 agent keys, rotation, nonces, signed
  envelopes; X25519 E2E encrypted messaging interoperable across SDKs.
- **FastAPI service**: signed data plane + admin control plane, automatic
  PostgreSQL schema migration, rate limiting, circuit breaker, optional Redis.
- **Web console**: React/Vite SPA served at `/app` with HttpOnly-cookie auth,
  i18n and theme support.
- **Observability**: Prometheus metrics (`/metrics`), OpenAPI spec
  (`/openapi.json`), health/readiness endpoints.
- **Multi-tenancy**: tenant records with tiers, agent binding, suspension and
  per-tier execution quotas.
- **SDKs**: Python (`vexinter.sdk` sync + `vexinter.remote` async) and
  TypeScript (`@vexinter-ai/sdk` on npm).

---

## Quickstart

### Docker Compose (recommended)

```bash
cp .env.example .env
# Edit .env: POSTGRES_PASSWORD and VEXINTER_ADMIN_TOKEN are required
docker compose up --build
```

Endpoints:

- **API / Console**: http://localhost:8000/app
- **Prometheus Metrics**: http://localhost:8000/metrics
- **OpenAPI Spec**: http://localhost:8000/openapi.json
- **Health**: http://localhost:8000/health (also `/ready`, `/live`, `/health/full`)

The compose file fails fast if required secrets are missing, does not expose
the PostgreSQL port, and runs the service as a non-root user with a
`HEALTHCHECK` and CPU/memory limits (`VEXINTER_CPUS` / `VEXINTER_MEMORY`).

**Production notes**

- **TLS**: the container does not terminate TLS, put a reverse proxy in
  front (Caddy/Nginx/Traefik). Session cookies default to `Secure` +
  `SameSite=Strict`; keep `VEXINTER_COOKIE_SECURE=1` behind HTTPS.
- **Secrets**: `.env` is fine for a single host; for orchestrated deploys
  prefer Docker secrets / your secret manager and drop `env_file`.
- **Scaling**: multiple replicas are safe for the API (Postgres-backed
  sequence guard and rate limiting), but keep `VEXINTER_SCHEDULER_INTERVAL_S`
  enabled on **one** replica only, follow-up scheduling is not
  leader-elected.

### Synchronous SDK

```python
from vexinter.sdk import Task
from vexinter.core.runtime import LocalRuntime
from vexinter.demo import CrmAdapter, BillingAdapter

rt = LocalRuntime([CrmAdapter(), BillingAdapter()])

with Task(
    rt,
    agent="soporte_bot",
    objective="Customer signup and invoice",
    condition={"op": "equals", "a": {"from": "crm", "path": "status"}, "b": "active"},
    input={"email": "client@example.com"}
) as t:
    c = t.tools.crm.create_customer({"name": "John Doe", "email": "client@example.com"})
    t.tools.billing.create_invoice({"customer_id": c.id, "amount": 150})
    verdict = t.verify()
    print(f"Status: {verdict.status}")  # "met" | "not_met" | "inconclusive"
```

### Async SDK

```python
import asyncio
from vexinter.remote import AsyncRemoteRuntime, AsyncRemoteTask
from vexinter.identity import AgentKeyMaterial

async def main():
    keys = AgentKeyMaterial.generate()
    async with AsyncRemoteRuntime("http://localhost:8000") as rt:
        async with AsyncRemoteTask(
            rt, agent="bot-async", objective="Asynchronous verification",
            condition={"op": "equals", "a": {"from": "system", "path": "ok"}, "b": True}
        ) as t:
            res = await t.tools.system.ping({"ping": True})
            print("Result:", res)

asyncio.run(main())
```

### GDPR erasure

```python
from vexinter.privacy import InMemoryKeyRing, PrivacyStore
from vexinter.store_pg import PostgresEventStore

inner = PostgresEventStore("postgresql://user:pass@localhost:5432/db")
store = PrivacyStore(inner, InMemoryKeyRing())

# Sensitive data is encrypted at rest and redacted in logs.
# Right to erasure (Art. 17 GDPR): destroys content, preserves integrity.
store.erase(execution_id="exec_12345", reason="data subject request")
assert store.verify_chain("task:exec_12345").ok
```

**Erasure limitations (read before relying on this for compliance):**

- `FileKeyRing.destroy()` overwrites the key file and deletes it, but a
  journaled filesystem or SSD does not guarantee _physical_ erasure,
  forensic recovery of the raw key bytes cannot be ruled out. For
  high-assurance deployments, keep the key directory on a separate
  volume excluded from backups, or front it with an HSM/KMS.
- Erasure renders content unreadable (crypto-shredding); the ciphertext
  itself remains in the append-only log. Whether encrypted-but-intact
  records satisfy a given erasure request is a legal question that
  depends on jurisdiction and retention policy, have counsel validate
  the approach for your deployment.
- Anti-replay sequence state for agent-to-agent envelopes is rebuilt
  from the persistent log on startup (`restore_from_log`), but it is
  per-process: multi-node deployments must route a given agent pair
  through a single node or add a shared sequence store.

---

## Installation

### Community (source)

```bash
git clone https://github.com/Vexinter-AI/vexinter.git
cd vexinter
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Paid plans (signed wheels via GitHub Packages)

Subscribers receive a personal access token (`read:packages`) by email after
purchase. Configure pip once:

```bash
pip config set global.index-url \
  https://__token__:<YOUR_GITHUB_PAT>@pypi.pkg.github.com/Vexinter-AI/simple
pip install vexinter
```

---

## Licensing and commercial model

Vexinter is **source-available** under the
[Community License (Fair Source)](LICENSE-COMMUNITY.md): free for
non-commercial use (personal projects, hobby, education, research,
evaluation, and nonprofit/government entities).

Commercial use, including **internal use inside for-profit organizations**
to automate business processes, requires a paid subscription
([Professional](https://vexinter.com/pricing) / Business). See the
[Terms of Service](TERMS-OF-SERVICE.md) and the
[license page](https://vexinter.com/license).

Paid plans add: signed wheels and updates via private GitHub Packages,
higher task quotas, priority/dedicated support, and signed license documents
that enforce plan limits automatically on the instance.

### How enforcement works

Each customer gets an Ed25519-signed license document served by the vendor's
billing endpoint (`GET /license/{customer_id}.json`). The instance fetches it
at most once per day, verifies the signature against a bundled public key,
caches the last valid document, and applies the resulting plan, so a
cancelled subscription degrades to Community limits within ~24h with no
client action. Validation is fully offline; the vendor backend only _issues_
documents.

The vendor-side commercial backend (payment webhooks → package credentials →
transactional emails → license issuing) is operated by Vexinter as a managed
service; customers never need to run or configure it.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      AGENT / CLIENT                         │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  vexinter.sdk.Task      vexinter.remote.AsyncRemoteTask │  │
│  └─────────────────────────────────────────────────────┘    │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS + Ed25519 Signed Requests
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    VEXINTER SERVICE (FastAPI)               │
│  ┌──────────────────┐  ┌────────────────────────────────┐   │
│  │ Data Plane       │  │ Control Plane (Admin)          │   │
│  │ /v1/tasks        │  │ /v1/identity/*                 │   │
│  │ /v1/messages     │  │ /v1/cases, /v1/followups       │   │
│  │ /v1/memory       │  │ /v1/metrics, /v1/checkpoints   │   │
│  └──────────────────┘  └────────────────────────────────┘   │
│                           │                                  │
│              ┌────────────┴────────────┐                     │
│              ▼                         ▼                     │
│      ┌─────────────┐           ┌──────────────┐             │
│      │ PostgreSQL  │           │  Console     │             │
│      │ (Events +   │           │  React/Vite  │             │
│      │  Hashchain) │           │  (/app)      │             │
│      └─────────────┘           └──────────────┘             │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

| Variable                                                                        | Required      | Description                                                  |
| ------------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------ |
| `POSTGRES_PASSWORD`                                                             | Yes (compose) | PostgreSQL password                                          |
| `VEXINTER_PG_DSN`                                                               | No\*          | PostgreSQL DSN; defaults to `InMemoryEventStore`             |
| `VEXINTER_AUTO_MIGRATE`                                                         | No            | `"1"` (default) applies schema on startup                    |
| `VEXINTER_ADMIN_TOKEN`                                                          | No\*\*        | Control-plane token; without it the service runs in dev mode |
| `VEXINTER_DEV_MODE`                                                             | No            | `"1"` disables auth, **development only**                    |
| `VEXINTER_ADAPTERS`                                                             | No            | `"module:Class,other:Factory"` custom connectors             |
| `VEXINTER_OPERATOR_TOKENS`                                                      | No            | `name:token[:role[:expires]]` for decision attribution       |
| `VEXINTER_COOKIE_SECURE`                                                        | No            | `"1"` for `Secure` cookies behind HTTPS                      |
| `VEXINTER_METRICS_TOKEN`                                                        | No            | Read-only token for `/metrics` (12+ chars)                   |
| `VEXINTER_LOGIN_MAX_FAILS` / `VEXINTER_LOGIN_LOCK_S`                            | No            | Login lockout (default: 5 / 900s)                            |
| `VEXINTER_REDIS_URL`                                                            | No            | Optional Redis for rate limit, circuit breaker, coordination |
| `VEXINTER_GDPR_INDEX_PATH`                                                      | No            | Disk cache for the GDPR person index (event-sourced)        |
| `VEXINTER_GDPR_LOG_PATH`                                                        | No            | Optional JSONL mirror of completed GDPR erasures            |

With `VEXINTER_PG_DSN` set, the event log is PostgreSQL-backed and
`TierUsage` quotas, `TenantService` membership/tier state, and the GDPR
person index and erasure requests all survive restarts and work across
multiple workers. With `InMemoryEventStore` they are per-process only.
| `VEXINTER_RATE_LIMIT_RPS` / `VEXINTER_RATE_LIMIT_BURST`                         | No            | Middleware rate limit                                        |
| `VEXINTER_KEYRING_DIR`                                                          | No            | File keyring dir for `PrivacyStore`                          |
| `VEXINTER_KEYS_DB_PATH` / `VEXINTER_KEYS_PASSWORD` / `VEXINTER_KEYS_SALT`       | No            | Encrypted agent-key persistence                              |
| `VEXINTER_LICENSE_BASE_URL` / `VEXINTER_LICENSE_ID` / `VEXINTER_LICENSE_PUBKEY` | No            | Signed license endpoint, customer id, Ed25519 public key     |
| `VEXINTER_LICENSE_CACHE`                                                        | No            | Cache file for the last valid license document               |
| `VEXINTER_DEFAULT_PLAN`                                                         | No            | Plan when no license doc applies (default: `community`)      |

See `.env.example` for the complete annotated list.

### Authentication

- **SPA / console**: `POST /v1/auth/login` exchanges the token for an
  **HttpOnly session cookie** (`vex_spa`, HMAC-signed, `SameSite=Lax`,
  optional `Secure`, 8h). The token is never stored in `localStorage`.
- **Non-browser clients** (SDK, scripts, Prometheus): `X-Vex-Admin` header or
  `Authorization: Bearer`.

### Multi-tenancy

Tenant records (name, tier, status, members with roles, per-tier quotas) live
in `vexinter/core/multitenancy.py` and are enforced by the service:

- **Agent binding**: `POST /v1/admin/tenants/{id}/agents`, bound agents
  inherit tenant status; suspending a tenant rejects its agents with 403.
- **Execution quota**: `POST /v1/tasks` returns 429 past the tier's daily cap.
- **Admin API** (admin token required): `POST/GET /v1/admin/tenants`,
  `GET /v1/admin/tenants/{id}`, `POST .../suspend|activate`, members and agent
  bind/unbind endpoints.
- Tenant lifecycle events go to the append-only audit log.
- **Persistence**: with `VEXINTER_PG_DSN` (Postgres store) tenants, members,
  agent bindings and daily usage counters live in `vex_tenant*` tables and
  survive restarts; usage increments are atomic (`INSERT ... ON CONFLICT`).
  With the in-memory store everything stays process-local, as before.

> **Isolation scope**: enforcement is per-agent, a credential resolves to an
> agent, the agent to its tenant, and the tenant's status/quota apply. The
> event store itself is a single shared log: queries are not row-filtered by
> tenant, so cross-tenant reads are prevented by credential scoping, not by
> storage-level separation.

---

## Observability

Prometheus metrics are derived exclusively from the append-only event log:

```
vexinter_executions_total
vexinter_final_states{state="met|not_met|inconclusive|..."}
vexinter_false_successes_total
vexinter_discrepancies_total
vexinter_retries_compensations_total
vexinter_human_decisions_requested_total / _made_total
vexinter_rejected_operations_total
vexinter_authentication_errors_total
vexinter_integrity_failures_total
vexinter_message_rejections / vexinter_tool_rejections (by reason)
```

---

## Tests

```bash
pytest -v                              # full suite (testcontainers + Docker)
pytest -m "not pg and not live" -v     # unit tests only (no DB)
cd packages/sdk-ts && npm test         # TypeScript SDK tests
```

Current result: **322 passed, 5 skipped** (Python) + SDK tests. `ruff` and
`mypy` are clean.

---

## Project structure

```
Vexinter/
├── packages/sdk-ts/        # TypeScript SDK (@vexinter-ai/sdk, public npm)
├── vexinter/               # Python package
│   ├── core/               # events, store, crypto, state machine, verifier,
│   │                       #   licensing, multitenancy, gdpr_erasure, ...
│   ├── identity/           # Ed25519 identity, envelopes, messaging
│   ├── privacy/            # PrivacyStore, KeyRing, E2E encryption
│   ├── remote.py           # AsyncRemoteRuntime / AsyncRemoteTask
│   ├── sdk.py              # Task / LocalRuntime high-level SDK
│   ├── store_pg.py         # PostgreSQL EventStore + SequenceGuard
│   ├── demo.py             # Example adapters (CRM, Billing)
│   └── app/                # Web console (React + Vite + Tailwind)
├── services/app.py         # FastAPI server (API + console + metrics + webhooks)
├── sql/schema.sql          # PostgreSQL schema
├── tests/                  # pytest suite (unit + PG integration)
├── examples/               # demos and benchmarks
├── .github/workflows/      # CI (ruff+mypy+pytest), publish, sdk-ts release
├── Dockerfile / docker-compose.yml / .env.example
├── LICENSE-COMMUNITY.md   # Fair Source license (free → MIT after 2y)
├── TERMS-OF-SERVICE.md     # Commercial terms
└── SECURITY.md             # Vulnerability reporting policy
```

---

## Links

- Website & pricing: https://vexinter.com
- Terms of Service: https://vexinter.com/terms
- License: https://vexinter.com/license
- Security policy: [SECURITY.md](SECURITY.md)

## Support

- Community: GitHub issues on `Vexinter-AI/vexinter`
- Paid plans: `support@vexinter.com` (Professional: 48h response SLA,
  Business: 8h response SLA). Include your **customer ID** (`lic_…`,
  shown in your welcome email and as `VEXINTER_LICENSE_ID` in `.env`)
  so we can verify your plan and apply the SLA — it is also visible in
  the footer of every billing email.

---

_v0.5.1 — November 2026_
