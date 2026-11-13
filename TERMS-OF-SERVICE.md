# Terms of Service — Vexinter

**Version:** 1.0 | **Effective date:** November 7, 2026

---

## 1. Parties and Scope

These Terms of Service ("Agreement") govern the relationship between
**Vexinter** (hereinafter, "We", "Licensor") and you ("Customer", "User",
"Subscriber") with respect to access to and use of Vexinter's **official
binaries, updates, private package repositories and associated services**
(collectively, the "Commercial Services").

By completing the purchase process on Polar and checking the
"I accept the Terms of Service" box, you agree to be bound by this Agreement.

**This Agreement does NOT cover the public source code** of the repository
`github.com/Vexinter-AI/vexinter`, which is governed by the
`LICENSE-COMMUNITY.md` license (Fair Source / Non-Commercial).

---

## 2. Key Definitions

- **Community Plan:** Free access to the source code under
  `LICENSE-COMMUNITY.md`. No right to commercial binaries, security
  updates or deployment in commercial production.

- **Professional Subscription:** Paid plan that grants commercial usage
  rights to the binary (`.whl`), access to the private `vexinter-pro` package,
  updates and email support (48h SLA).

- **Business Subscription:** Paid plan with expanded limits, priority
  support (Slack Connect, 8h SLA), certified connectors and advanced
  cost control policies.

- **Billing Period:** Monthly or Annual (annual billing is ~20% cheaper
  than paying monthly).

- **License Token:** Secret per-customer credential delivered by email.
  It authenticates requests to the license-document and package-download
  endpoints (sent as `Authorization: Bearer`, or via `~/.netrc` for
  `pip`). It is never placed in URLs. The customer id (`lic_…`) is a
  public identifier, not a credential.

- **PAT (Personal Access Token):** GitHub token with `read:packages`
  scope automatically generated for access to the private source
  repository.

- **Tasks:** Units of work counted toward plan limits. A task is counted
  when its objective is accepted by the service (`POST /v1/tasks`) —
  not when it finishes. Retries, tool calls and verification rounds
  inside a task do not consume extra quota. Failed, aborted and
  cancelled tasks count the same as successful ones.

  A task is **not** counted when it is rejected before acceptance:
  payload validation errors (4xx), authorization failures, tenant quota
  rejections and tier-limit denials consume no quota. Creation is
  idempotent: retrying `POST /v1/tasks` with the same `idempotency_key`
  returns the originally created task (`deduplicated: true`) instead of
  consuming a second unit; reusing the key with a different payload is
  rejected with `409`.

  **Delegated sub-tasks** (`POST /v1/tasks/{id}/subtasks`, including
  cascades where a sub-task delegates further) do **not** consume task
  quota: they execute inside the parent task's quota unit, inherit the
  parent's budget and are bounded by `VEXINTER_MAX_SUBTASK_DEPTH`
  (default 3) and `VEXINTER_MAX_SUBTASKS_PER_TASK` (default 32).

- **Execution guardrails:** Every task execution is bounded by
  conservative defaults: `VEXINTER_MAX_STEPS` tool-call attempts
  (default 100) and `VEXINTER_TASK_DEADLINE_S` seconds (default 1800),
  both overridable per task via `max_steps` / `deadline_s`. Loop
  detection covers identical consecutive calls
  (`VEXINTER_LOOP_THRESHOLD`, default 3), repeating call-pattern cycles
  (`VEXINTER_CYCLE_THRESHOLD`, default 3) and windows without new
  outcomes (`VEXINTER_NO_PROGRESS_WINDOW`, default 8). Per-task budgets
  (`max_tokens`, `max_usd`, `model_pricing`) are optional; `max_tokens`
  is the primary bound because `max_usd` is only as reliable as the
  client-supplied pricing table. On the Community plan, `max_tokens` is
  capped at `VEXINTER_COMMUNITY_MAX_TOKENS` (default 50,000) per task.

- **Overage:** Usage that exceeds the monthly limit included in the plan.

---

## 3. Commercial License Grant (Active Subscription)

Subject to timely payment of the applicable fees and compliance with this
Agreement, we grant you a **non-exclusive, non-transferable and revocable
license** to:

1. **Download, install and run** the official Vexinter binaries
   (distributed as versioned wheels from our signed package endpoint)
   in your own commercial production environments.

2. **Access updates** (security patches, bug fixes, new minor and major
   versions) while the subscription is active.

3. **Use the REST API and SDK** against licensed self-hosted instances.

4. **Register agents** up to the limits of the contracted plan.

**This license expires automatically** upon cancellation, non-renewal or
failure to pay for the subscription.

---

## 4. Plans, Limits and Pricing (USD)

| Feature                     | Community        | Professional        | Business             |
|-----------------------------|------------------|---------------------|----------------------|
| **Price Monthly (Annual)**  | $0               | **$79/mo** ($948/yr) | **$299/mo** ($3,588/yr) |
| **Price Monthly (Monthly)** | $0               | $99/mo              | $379/mo              |
| **Tasks / month**              | 500 (hard limit) | 5,000 (hard limit) | 50,000 (hard limit) |
| **Overage (/1k extra tasks)** | **Not available** | **Not available** | **Not available** |
| **Registered Agents**       | 3                | Unlimited           | Unlimited            |
| **Concurrent agent instances** | Not enforced   | 5                   | Unlimited            |
| **Privacy / GDPR**          | InMemory only    | Included (PrivacyStore) | Included + Audit     |
| **Certified MCP Connectors** | Core only (Demo) | 5 included          | Full catalog         |
| **Cost Control / Circuit Breaker** | ❌          | Basic               | Advanced (Budgets per Agent) |
| **Support**                 | Community (GitHub) | Email (48h SLA)   | Priority / Slack (8h SLA) |
| **Deployment**              | Docker Compose   | Docker Compose / K8s Helm | Helm / Terraform / Multi-region |

> **Note:** "Tasks" limits are evaluated monthly
> (natural cycle from the 1st to the last day of the month). A task
> consumes one unit at creation time, regardless of its final outcome.
>
> - **All plans**: *Hard block* at the monthly task limit. When the
>   limit is reached, the runtime rejects **new** task creation with
>   error `QUOTA_EXCEEDED` until the next cycle. Tasks already running
>   are never interrupted — they finish under their own budget and
>   guardrail limits. There is no overage or pay-per-extra-use on any
>   plan. `POST /v1/tasks` responses carry a `warnings` entry once
>   usage reaches 80% of the quota.
>
> - **Enforcement scope:** on self-hosted deployments the monthly
>   counter lives in the deployment's own database. Resetting the
>   database or running additional unconnected instances restarts the
>   count — this is an accepted limitation of self-hosting (the license
>   remains the legal enforcement). The hosted service enforces quotas
>   server-side.

---

## 5. Authentication and Package Access

1. When activating a **Professional** or **Business** subscription, our
   automated system (via Polar and our billing pipeline) issues, by
   email:
   - A **license token** — the secret credential for the license-document
     and package-download endpoints. It is sent as `Authorization:
     Bearer` by the SDK and via `~/.netrc` for `pip`; it must never be
     placed in URLs.
   - The **customer id** (`lic_…`) — a public identifier used to look up
     the license document; it is not a credential by itself.
   - A **GitHub PAT** (`read:packages`, 31-day expiry, auto-renewed)
     for access to the private source repository.

2. **Package downloads:** the `vexinter-pro` wheel is served from our
   billing endpoint and only while the signed license document reports
   an active, trial or past-due status. Configure `~/.netrc`
   (`chmod 600`):

   ```
   machine billing.vexinter.com login lic_xxxxxxxxxxxxxxxx password <token>
   ```

   then `pip install https://billing.vexinter.com/pro/vexinter_pro-<ver>-py3-none-any.whl`.

3. **Prohibited:** Sharing, reselling, publishing or allowing the
   license token or PAT to be used by unauthorized third parties.
   Detection of anomalous usage (multiple IPs, massive downloads) may
   trigger immediate revocation.

4. **Instance binding:** the signed license document carries a
   `max_instances` cap (Professional: 5 concurrent agent instances,
   Business: unlimited). The runtime enforces it at instance-spawn time;
   exceeding it returns `403` until an instance is revoked or the plan
   is upgraded.

5. Upon subscription cancellation, credentials are **revoked** within a
   maximum of 24 hours after the paid period ends. Artifacts already
   downloaded **lose the right to updates and security patches**, and
   the instance degrades to Community limits via the signed license
   document (see §7).

---

## 6. Billing, Payments and Renewal

1. **Payment processor:** Polar (Merchant of Record). They
   handle VAT/Taxes, invoices, refunds and card disputes.

2. **Billing cycle:** Charged in advance at the start of each
   period (monthly or annual). Annual billing is approximately 20%
   cheaper than paying monthly.

3. **Automatic renewal:** Subscriptions renew automatically
   unless cancelled beforehand (see §7).

4. **Taxes:** Prices do not include applicable taxes (VAT, GST,
   Sales Tax). Polar adds them according to your jurisdiction.

5. **No pro-rata refunds:** No refunds are issued for unused partial
   periods after cancellation. Access remains until
   the end of the already-paid period.

---

## 7. Cancellation and Effects

1. **Cancellation at any time:** From the Polar customer portal
   or by contacting `billing@vexinter.com`.

2. **Immediate effects after cancellation:**
   - No new future charges are generated.
   - Access to updates and package downloads continues **until the
     end of the already-paid period**.
   - The PAT is revoked **24 hours after the end of the paid period**.

3. **Effects after expiration of the paid period:**
   - You lose the right to run the binaries in **commercial production
     environments**.
   - You must migrate to the **Community** edition (source code,
     non-commercial only) or renew the subscription.
   - Downloaded binaries are **not technically "deactivated"** (there is
     no runtime DRM). Plan limits may be enforced automatically through
     signed license documents we publish for your customer id, which degrade
     the instance to Community limits
     within approximately 24 hours of cancellation or payment failure.
     In any case, using the binaries commercially without an active
     license constitutes **breach of contract and copyright
     infringement**.

---

## 8. Intellectual Property

1. **Source code:** Remains the property of Vexinter and contributors.
   Community License = Fair Source (non-commercial). Commercial License =
   right to use the binary while you pay.

2. **Trademarks:** "Vexinter", the logo and product names are registered
   trademarks. You may not use them to promote derivative services without
   written authorization.

3. **Customer data:** You retain all rights to your data,
   executions, events and memory generated while using the Software. We
   do not access them in self-hosted deployments.

---

## 9. Warranties, Disclaimers and Limitation of Liability

1. **No warranties:** THE SOFTWARE AND SERVICES ARE PROVIDED "AS IS",
   WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED,
   INCLUDING MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
   NON-INFRINGEMENT.

2. **Limitation of liability:** OUR TOTAL CUMULATIVE LIABILITY
   SHALL NOT EXCEED THE AMOUNT PAID BY YOU IN THE 12 MONTHS PRECEDING
   THE CLAIM (OR $100 USD, WHICHEVER IS GREATER).

3. **Exclusion of indirect damages:** IN NO EVENT SHALL WE BE LIABLE
   FOR INDIRECT, INCIDENTAL, CONSEQUENTIAL DAMAGES, LOSS OF
   PROFITS, DATA OR OPPORTUNITIES, EVEN IF WE WERE ADVISED OF THEIR
   POSSIBILITY.

4. **Force majeure:** We are not liable for delays or failures due to
   causes beyond our reasonable control.

---

## 10. Privacy and Data Protection (GDPR / LOPD)

1. **Billing data:** Managed by Polar (their privacy policy
   applies). We receive: email, name, plan, status.

2. **PAT and GitHub:** The PAT is generated via the GitHub API; your
   GitHub username is stored temporarily for generation and revocation.

3. **Telemetry:** The Commercial edition does **NOT send telemetry** to
   our servers. Prometheus metrics (`/metrics`) are local to your
   deployment.

4. **DPA (Data Processing Addendum):** Available upon request for
   Business customers who require it contractually.

---

## 11. Support and Maintenance

| Plan           | Channel               | Initial Response Time | Hours               |
|----------------|-----------------------|-----------------------|---------------------|
| Professional   | Email (`support@`)    | 48 business hours     | Mon-Fri 9-18 CET    |
| Business       | Slack Connect / Email | 8 business hours      | Mon-Fri 9-18 CET    |

Support covers: installation, configuration, API usage, debugging of
product errors. To verify your plan and apply the SLA, include your
customer ID (`lic_…`) — found in your welcome email, in the footer of
every billing email, and as `VEXINTER_LICENSE_ID` in `.env`. The ID is
verified against the signed license document.

**It does not cover:** development of custom adapters,
integration into your infrastructure, or architecture consulting (unless
under a separate contract).

---

## 12. Governing Law and Dispute Resolution

1. **Governing law:** The laws of the **Argentine Republic**, without
   regard to conflict-of-laws rules.

2. **Dispute resolution:**
   - Good-faith attempt via email (`legal@vexinter.com`) within 30 days.
   - Failing that, disputes shall be submitted to the **ordinary courts of
     the City of Buenos Aires**, Argentina, which have exclusive
     jurisdiction, **unless** mandatory consumer-protection law in your
     place of residence provides otherwise.

3. **Language:** The English version prevails in case of conflict with
   translations.

---

## 13. Modifications

We may update these Terms with **30 days' notice** by email and by
publishing the new version at `vexinter.com/terms`. Continued use after
the effective date constitutes acceptance. If you do not agree, you may
cancel before the effective date without penalty.

---

## 14. General Provisions

- **Assignment:** You may not assign this Agreement without our written
  consent. We may assign it to an affiliate or in the event of a
  merger/acquisition.
- **Severability:** If a clause is void, the remaining clauses remain in
  effect.
- **Waiver:** Failure to exercise a right does not constitute a waiver
  thereof.
- **Entire agreement:** This document + `LICENSE-COMMUNITY.md` + pricing
  page + Polar purchase confirmation constitute the entire
  agreement.

---

## 15. Contact

**Vexinter**  
Email: `legal@vexinter.com` | `billing@vexinter.com` | `support@vexinter.com`  
Web: `https://vexinter.com`

---

*End of Terms of Service v1.0*
