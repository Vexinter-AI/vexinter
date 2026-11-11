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
  rights to the binary (`.whl`), access to private GitHub Packages,
  updates and email support (48h SLA).

- **Business Subscription:** Paid plan with expanded limits, priority
  support (Slack Connect, 8h SLA), certified connectors and advanced
  cost control policies.

- **Billing Period:** Monthly or Annual (with 2 months free / equivalent
  to a 16.7% discount).

- **PAT (Personal Access Token):** GitHub token with `read:packages`
  scope automatically generated to authenticate downloads from
  `pypi.pkg.github.com/Vexinter-AI`.

- **Verified Tasks:** Task executions that complete the verification
  cycle (§4 of the Vexinter protocol) and count toward the plan limits.

- **Overage:** Usage that exceeds the monthly limit included in the plan.

---

## 3. Commercial License Grant (Active Subscription)

Subject to timely payment of the applicable fees and compliance with this
Agreement, we grant you a **non-exclusive, non-transferable and revocable
license** to:

1. **Download, install and run** the official Vexinter binaries
   (distributed as signed Python wheels `.whl` on GitHub Packages)
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
| **Verified Tasks / month**  | 100 (hard limit) | 5,000               | 50,000               |
| **Overage (/1k extra tasks)** | **Not available** (hard limit) | **$5 / 1k** (soft limit) | **$3 / 1k** (soft limit) |
| **Registered Agents**       | 3                | Unlimited           | Unlimited            |
| **Privacy / GDPR**          | InMemory only    | Included (PrivacyStore) | Included + Audit     |
| **Certified MCP Connectors** | Core only (Demo) | 5 included          | Full catalog         |
| **Cost Control / Circuit Breaker** | ❌          | Basic               | Advanced (Budgets per Agent) |
| **Support**                 | Community (GitHub) | Email (48h SLA)   | Priority / Slack (8h SLA) |
| **Deployment**              | Docker Compose   | Docker Compose / K8s Helm | Helm / Terraform / Multi-region |

> **Note:** "Verified Tasks" limits are evaluated monthly
> (natural cycle from the 1st to the last day of the month).
>
> - **Community**: *Hard block* at 100 tasks/month. When the limit is
>   reached, the runtime rejects new tasks with error `QUOTA_EXCEEDED`
>   until the next cycle. There is no overage or pay-per-extra-use.
> - **Professional / Business**: *Soft block* with billable overage.
>   When the included limit is exceeded, execution may continue and the
>   excess is billed at $5/1k (Prof) or $3/1k (Biz) at month close.

---

## 5. Authentication and Package Access (GitHub Packages)

1. When activating a **Professional** or **Business** subscription, our
   automated system (via Polar and our automated billing pipeline) generates a
   **personal PAT** on GitHub in your name with:
   - Scope: `read:packages` (read-only access to private packages).
   - Expiration: **31 days** (automatically renewed each month while
     the subscription is current).
   - Note: `Vexinter-Sub-{email}-{plan}`.

2. **You are responsible** for configuring `pip` / `uv` / `poetry` to use
   said token. Instructions are sent by email after purchase.

3. **Prohibited:** Sharing, reselling, publishing or allowing the PAT to
   be used by unauthorized third parties. Detection of anomalous usage
   (multiple IPs, massive downloads) may trigger immediate revocation.

4. Upon subscription cancellation, the PAT is **automatically revoked**
   within a maximum of 24 hours. Binaries already downloaded **lose the
   right to updates and security patches**.

---

## 6. Billing, Payments and Renewal

1. **Payment processor:** Polar (Merchant of Record). They
   handle VAT/Taxes, invoices, refunds and card disputes.

2. **Billing cycle:** Charged in advance at the start of each
   period (monthly or annual). Annual billing offers 2 months free
   (pay 10 months, receive 12).

3. **Automatic renewal:** Subscriptions renew automatically
   unless cancelled beforehand (see §7).

4. **Overage:** Calculated at the close of each calendar month and
   charged on the following invoice (or an immediate charge if there is
   no pending invoice).

5. **Taxes:** Prices do not include applicable taxes (VAT, GST,
   Sales Tax). Polar adds them according to your jurisdiction.

6. **No pro-rata refunds:** No refunds are issued for unused partial
   periods after cancellation. Access remains until
   the end of the already-paid period.

---

## 7. Cancellation and Effects

1. **Cancellation at any time:** From the Polar customer portal
   or by contacting `billing@vexinter.com`.

2. **Immediate effects after cancellation:**
   - No new future charges are generated.
   - Access to updates and GitHub Packages continues **until the
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

**ANNEX A — `pip` Configuration Instructions (reference)**

After purchase, you will receive an email with your personal PAT.
Configure your package client:

```bash
# Option 1: Environment variable (recommended for CI/CD)
export VEXINTER_PAT="ghp_xxxxxxxxxxxx"
pip config set global.extra-index-url https://${VEXINTER_PAT}@pypi.pkg.github.com/Vexinter-AI/

# Option 2: .pypirc file (user)
cat <<EOF > ~/.pypirc
[distutils]
index-servers = vexinter

[vexinter]
repository = https://pypi.pkg.github.com/Vexinter-AI/
username = YOUR_GITHUB_USERNAME
password = YOUR_PAT_HERE
EOF

# Option 3: uv (modern, fast)
uv pip install --index-url https://${VEXINTER_PAT}@pypi.pkg.github.com/Vexinter-AI/simple/ vexinter
```

**The PAT expires after 31 days and is automatically renewed** while your
subscription is active. If you rotate it manually, update your
configuration.

---

*End of Terms of Service v1.0*
