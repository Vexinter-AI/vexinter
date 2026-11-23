# Terms of Service: Vexinter

**Version:** 1.0 | **Effective date:** November 22, 2026

---

## 1. Parties and Scope

These Terms of Service ("Agreement") govern the relationship between
**Vexinter**, the commercial name of the product operated by
**Facundo Oyola** (Argentine Republic; see §15) (hereinafter, "We",
"Licensor") and you ("Customer", "User",
"Subscriber") with respect to access to and use of Vexinter's **official
binaries, updates, signed package endpoints and associated services**
(collectively, the "Commercial Services"). This Agreement is intended for
business and professional use; it is not a consumer contract.

By completing the purchase process on Polar and checking the
"I accept the Terms of Service" box, you agree to be bound by this Agreement.

**This Agreement does NOT cover the public source code** of the repository
`github.com/Vexinter-AI/vexinter`, which is governed by the
`LICENSE-COMMUNITY.md` license (source-available, non-commercial).

---

## 2. Key Definitions

- **Community Plan:** Free access to the source code under
  `LICENSE-COMMUNITY.md`. No right to commercial binaries, security
  updates or deployment in commercial production.

- **Professional Subscription:** Paid plan that grants commercial usage
  rights to the binary (`.whl`), access to the private `vexinter-pro` package,
  updates and email support (48 business-hour SLA).

- **Business Subscription:** Paid plan with expanded limits, priority
  support (Slack Connect, 8 business-hour SLA) and advanced cost control policies.

- **Billing Period:** Monthly or Annual (annual billing is ~20% cheaper
  than paying monthly).

- **License Token:** Secret per-customer credential delivered by email.
  It authenticates requests to the license-document and package-download
  endpoints (sent as `Authorization: Bearer`, or via `~/.netrc` for
  `pip`). It is never placed in URLs. The customer id (`lic_…`) is a
  public identifier, not a credential.

- **Tasks:** Units of work counted toward plan limits. A task is counted
  when its objective is accepted by the service (`POST /v1/tasks`),
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
  parent's budget and are bounded by `VEXINTER_MAX_SUBTASK_DEPTH` and
  `VEXINTER_MAX_SUBTASKS_PER_TASK`; default values are documented in
  the repository and subject to change.

- **Execution guardrails:** Every task execution is bounded by
  conservative defaults: `VEXINTER_MAX_STEPS` tool-call attempts and
  `VEXINTER_TASK_DEADLINE_S` seconds, both overridable per task via
  `max_steps` / `deadline_s`. Loop detection covers identical
  consecutive calls (`VEXINTER_LOOP_THRESHOLD`), repeating call-pattern
  cycles (`VEXINTER_CYCLE_THRESHOLD`) and windows without new outcomes
  (`VEXINTER_NO_PROGRESS_WINDOW`); default values are documented in the
  repository and subject to change. Per-task budgets (`max_tokens`,
  `max_usd`, `model_pricing`) are optional; `max_tokens` is the primary
  bound because `max_usd` is only as reliable as the client-supplied
  pricing table. On the Community plan, `max_tokens` is capped by
  `VEXINTER_COMMUNITY_MAX_TOKENS` per task.

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
| **Registered Agents**       | 3                | Unlimited           | Unlimited            |
| **Concurrent agent instances** | Not enforced   | 5                   | Unlimited            |
| **Privacy / GDPR**          | InMemory only    | Included (PrivacyStore) | Included + Audit     |
| **MCP Connectors**          | Core gateway      | 5 included          | Unlimited            |
| **Cost Control / Circuit Breaker** | Execution caps only | Basic budgets & guardrails | Advanced budgets & guardrail policies |
| **Support**                 | Community (GitHub) | Email (48 business-hour SLA) | Priority / Slack (8 business-hour SLA) |
| **Deployment**              | Docker Compose   | Docker Compose / Kubernetes | Assisted deployment (Helm, Terraform, multi-region on request) |

> **Note:** "Tasks" limits are evaluated monthly
> (natural cycle from the 1st to the last day of the month). A task
> consumes one unit at creation time, regardless of its final outcome.
>
> - **All plans**: *Hard block* at the monthly task limit. When the
>   limit is reached, the runtime rejects **new** task creation with
>   error `QUOTA_EXCEEDED` until the next cycle. Tasks already running
>   are never interrupted, they finish under their own budget and
>   guardrail limits. There is no overage or pay-per-extra-use on any
>   plan. `POST /v1/tasks` responses carry a `warnings` entry once
>   usage reaches 80% of the quota.
>
> - **Enforcement scope:** on self-hosted deployments the monthly
>   counter lives in the deployment's own database, so replacing or
>   resetting that store restarts the count; this is an accepted
>   limitation of self-hosting (the license remains the legal
>   enforcement).
>
> - **"Cost Control / Circuit Breaker"** refers to optional per-task
>   budgets (`max_tokens`, `max_usd`). The execution guardrails defined
>   in §2 (max steps, task deadline, token ceilings) apply to **every**
>   plan, including Community.

---

## 5. Authentication and Package Access

1. When activating a **Professional** or **Business** subscription, our
   automated system (via Polar and our billing pipeline) issues, by
   email:
   - A **license token**, the secret credential for the license-document
     and package-download endpoints. It is sent as `Authorization:
     Bearer` by the SDK and via `~/.netrc` for `pip`; it must never be
     placed in URLs.
   - The **customer id** (`lic_…`), a public identifier used to look up
     the license document; it is not a credential by itself.

2. **Package downloads:** the `vexinter-pro` wheel is served from our
   billing endpoint and only while the signed license document reports
   an active or past-due status. Configure `~/.netrc`
   (`chmod 600`):

   ```
   machine billing.vexinter.com login lic_xxxxxxxxxxxxxxxx password <token>
   ```

   then `pip install https://billing.vexinter.com/pro/vexinter_pro-<ver>-py3-none-any.whl`.

3. **Prohibited uses of the Commercial package.** You may not:

   - Share, resell, publish, rent or sublicense the license token,
     wheel package or signed license documents, or allow them to be used
     by unauthorized third parties;
   - Redistribute the `vexinter-pro` wheel or any part of the Commercial
     binaries, in original or modified form;
   - Alter, decompile or otherwise circumvent the license verification,
     signature checks, plan limits or any other technical protection
     measure;
   - Use the Software to build, offer or support a product or service
     that directly competes with Vexinter's commercial offerings;
   - Run more concurrent instances or consume more tasks than the
     contracted plan allows;
   - Misrepresent the source or licensing of the Software.

   Detection of anomalous usage (e.g. credential sharing across
   multiple IPs, abnormal download volumes) may trigger suspension or
   revocation of access, with notice to the customer when reasonably
   practicable.

4. **Instance binding:** the signed license document carries a
   `max_instances` cap (Professional: 5 concurrent agent instances,
   Business: unlimited). The runtime enforces it at instance-spawn time;
   exceeding it returns `403` until an instance is revoked or the plan
   is upgraded.

5. Upon subscription cancellation, credentials are **revoked** within a
   maximum of 24 hours after the **end of the paid period**. Artifacts
   already downloaded **lose the right to updates and security patches**,
   and the instance degrades to Community limits via the signed license
   document (see §7).

6. **Offline license:** Business customers operating in environments
   without outbound internet access may request a **signed offline
   license document**, delivered by email, valid for the corresponding
   paid period and renewable at each billing period. The document is
   verified locally by the runtime using an embedded public key and does
   not require connectivity to take effect. Requests may be sent to
   `billing@vexinter.com` with the customer id (`lic_…`).

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
   periods after cancellation, **except where mandatory law (including
   consumer-protection law) provides otherwise**. Access remains until
   the end of the already-paid period.

6. **Price changes:** we may update prices with at least 30 days' notice
   under §13. A new price applies from your next billing period; your
   current period is never repriced mid-cycle.

---

## 7. Cancellation and Effects

1. **Cancellation at any time:** From the Polar customer portal
   or by contacting `billing@vexinter.com`.

2. **Immediate effects after cancellation:**
   - No new future charges are generated.
   - Access to updates and package downloads continues **until the
     end of the already-paid period**.

3. **Effects after expiration of the paid period:**
   - You lose the right to run the binaries in **commercial production
     environments**.
   - You must migrate to the **Community** edition (source code,
     non-commercial only) or renew the subscription.
   - Downloaded binaries are **not technically "deactivated"** (there is
     no runtime DRM). Plan limits may be enforced automatically through
     signed license documents we publish for your customer id, which degrade
     the instance to Community limits
     within approximately 24 hours of the **end of the paid period** or
     of a payment failure. During the past-due retry window the license
     document keeps reporting your paid entitlements; degradation begins
     only after the paid period ends or the payment is definitively
     written off.
     In any case, using the binaries commercially without an active
     license constitutes **breach of contract and copyright
     infringement**.

---

## 8. Intellectual Property

1. **Source code:** Remains the property of Vexinter and contributors.
   Community License = source-available, non-commercial. Commercial License =
   right to use the binary while you pay.

2. **Trademarks:** "Vexinter", the logo and product names are trademarks
   of Vexinter. You may not use them to promote derivative services
   without written authorization.

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
   THE CLAIM (OR $100 USD, WHICHEVER IS GREATER), to the extent
   permitted by applicable law.

3. **Exclusion of indirect damages:** IN NO EVENT SHALL WE BE LIABLE
   FOR INDIRECT, INCIDENTAL, CONSEQUENTIAL DAMAGES, LOSS OF
   PROFITS, DATA OR OPPORTUNITIES, EVEN IF WE WERE ADVISED OF THEIR
   POSSIBILITY, except where such exclusion is not permitted by
   applicable law (including for willful misconduct or gross
   negligence).

4. **No guarantee of detection:** Vexinter provides verification,
   memory and communication primitives and evidence tooling; it does
   not guarantee that every failure, misuse or false success will be
   detected. A `met` verdict means the declared condition held against
   the recorded state — it is evidence, not a guarantee that the agent
   behaved correctly or that the condition captured your intent. It is
   not a substitute for human oversight and should not be relied upon
   as the sole safeguard for critical operations.

5. **Force majeure:** We are not liable for delays or failures due to
   causes beyond our reasonable control.

---

## 10. Privacy and Data Protection (GDPR / Argentine Personal Data Protection Act No. 25.326)

The full Privacy Policy is published at `vexinter.com/privacy`.

1. **Billing data:** Managed by Polar (their privacy policy
   applies). We receive: email, name, plan, status.

   **Tax and invoicing:** we use your billing data (email, name,
   country and purchase details) to issue invoices and comply with
   tax obligations, this is a legal requirement, not optional. If
   you provide fiscal data (e.g. tax ID, fiscal address) it is used
   solely for invoicing and retained as required by tax law.

2. **Telemetry:** The Commercial edition does **NOT send usage telemetry**
   to our servers. Prometheus metrics (`/metrics`) are local to your
   deployment.

   **License checks:** the runtime periodically fetches your signed
   license document from `billing.vexinter.com` (approximately once per
   day of active use; a cached copy is used for up to 7 days if the
   endpoint is unreachable). If the endpoint remains unreachable beyond
   that cache window, the instance **degrades to Community limits**
   until connectivity is restored and a valid license document can be
   fetched again; customers operating without reliable outbound
   connectivity should request an offline license under §5.6. These
   requests necessarily expose your IP
   address and customer id (`lic_…`) to our billing infrastructure,
   used for license enforcement, security and abuse prevention only,
   never for usage profiling. Access logs are retained for up to 90 days
   and then deleted; signed license documents are kept as needed for
   license enforcement, commission accounting and tax/legal obligations.

3. **Referral attribution:** If you purchase after following a creator
   referral link, we store a record of the paid order (referral code,
   order/subscription identifiers, plan, amount, currency, timestamps)
   to calculate commissions. No additional personal data beyond your
   purchase metadata is collected for this purpose.

4. **DPA (Data Processing Addendum):** Available upon request for
   Business customers who require it contractually.

---

## 11. Support and Maintenance

| Plan           | Channel               | Initial Response Time | Hours               |
|----------------|-----------------------|-----------------------|---------------------|
| Professional   | Email (`support@`)    | 48 business hours     | Mon-Fri 9-18 ART (UTC−3) |
| Business       | Slack Connect / Email | 8 business hours      | Mon-Fri 9-18 ART (UTC−3) |

Support covers: installation, configuration, API usage, debugging of
product errors. "Business hours" means Monday–Friday 9-18 ART
(UTC−3), excluding Argentine national holidays and operator absences.
Response-time SLAs are counted in business hours. To verify your plan
and apply the SLA, include your
customer ID (`lic_…`), found in your welcome email, in the footer of
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
     jurisdiction, **unless** (a) mandatory consumer-protection law in
     your place of residence provides otherwise, or (b) both parties
     agree in writing to resolve the dispute by binding arbitration.

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
- **Indemnification:** You agree to indemnify and hold us harmless
  from claims arising out of your use of the Software in violation of
  this Agreement or applicable law.
- **Survival:** Clauses that by their nature should survive termination
  (liability, indemnification, governing law, disclaimers) survive.
- **Third-party components:** the Software may include third-party
  components under their own licenses; those licenses govern the
  respective components.
- **Export compliance:** You are responsible for compliance with
  applicable export-control laws when using or redistributing the
  Software.
- **Pricing page:** changes to the pricing page published at
  `vexinter.com/#pricing` are effective on publication and constitute
  modifications under §13.
- **Entire agreement:** This document + `LICENSE-COMMUNITY.md` + pricing
  page + Polar purchase confirmation constitute the entire
  agreement.

---

## 15. Contact and Counterparty Identification

**Vexinter**, operated by **Facundo Oyola** (Argentine Republic).

For legal notices and notifications, contact:

**Facundo Oyola**  
Legal notices: `legal@vexinter.com` (deemed received upon confirmed
delivery, or 48 hours after sending if no delivery failure is notified)

Additional supplier identification details (tax identifiers, domicile)
are available upon request at `legal@vexinter.com`.

Email: `legal@vexinter.com` | `billing@vexinter.com` | `support@vexinter.com`  
Web: `https://vexinter.com`

---

*End of Terms of Service v1.0*
