# Privacy Policy

**Effective date:** November 24, 2026
**Controller:** Facundo Oyola (Vexinter), Buenos Aires, Argentina
**Contact:** legal@vexinter.com

This policy explains what personal data Vexinter processes, why, and
the rights you have under the GDPR and Argentine Personal Data
Protection Act No. 25.326.

---

## 1. The short version

If you self-host Vexinter, **your agents' data never reaches us**. The
software runs in your infrastructure; we cannot access it. The only
personal data we process is what is needed to sell, license and
support the product: billing details, license identifiers and support
correspondence.

---

## 2. What we process and why

### 2.1 Billing and subscription data

Purchases are processed by **Polar** as Merchant of Record. Polar
collects and processes your payment details (card data, billing
address, tax identifiers) under its own privacy policy. We receive
from Polar: your email, name, plan, subscription status and order
identifiers. We use this to:

- deliver the license and package access you purchased;
- issue invoices and comply with tax obligations (legal requirement);
- provide support and enforce the Terms of Service.

### 2.2 License enforcement data

A licensed instance fetches its signed license document from
`billing.vexinter.com` approximately once per day of active use. These
requests expose your **IP address** and **customer id (`lic_…`)** to
our billing infrastructure. They are used for license enforcement,
security and abuse prevention only — never for usage profiling.
Access logs are retained for up to **90 days** and then deleted.
Signed license documents are kept as needed for license enforcement,
commission accounting and tax/legal obligations.

### 2.3 Referral attribution

If you purchase after following a creator referral link, we store the
paid order's metadata (referral code, order/subscription identifiers,
plan, amount, currency, timestamps) to calculate commissions. No
additional personal data is collected for this purpose.

### 2.4 Support and correspondence

When you contact us (support, sales, legal), we process the content
of your messages and your contact details to respond and to keep
records of the interaction.

### 2.5 Self-hosted deployments

The Community and Commercial editions running on your infrastructure
process agent data, payloads and keys **on your systems**. We have no
access to them and do not receive telemetry from them. The only
outbound connection is the daily license check described in §2.2,
which carries no task or agent data.

### 2.6 Website

This website is a static site hosted on Cloudflare Pages. We do not
run analytics or tracking scripts. Cloudflare may process standard
technical request data (IP address, user agent) to deliver and protect
the site, under its own privacy policy.

---

## 3. Cookies

- **vexinter.com**: no tracking cookies.
- **Your own deployments**: the operator console sets a `vex_spa`
  HttpOnly session cookie after login. That cookie is set by *your*
  instance, not by us, and is governed by your own privacy notices.

---

## 4. Subprocessors

| Provider | Purpose | Data involved |
| --- | --- | --- |
| Polar | Payments, Merchant of Record, invoicing | Payment and billing data |
| Cloudflare | DNS, website hosting, billing Worker, security | Technical request data, license-check logs |
| GitHub / npm | Source code and SDK distribution | Account data held by those platforms |
| Email provider | Transactional email (welcome, license delivery) | Email address, name |

We do not sell personal data and we do not share it with advertisers.

---

## 5. International transfers

Our processors operate primarily in the United States and the European
Union. Where required, transfers rely on adequacy decisions or
appropriate safeguards (e.g. Standard Contractual Clauses). Argentine
Personal Data Protection Act No. 25.326 requirements are observed for
transfers originating in Argentina.

---

## 6. Retention

- **Billing records**: retained as required by tax and accounting law.
- **License-check access logs**: up to 90 days.
- **Support correspondence**: retained for the duration of the
  relationship plus a reasonable period for legal claims.
- **Referral records**: retained while the referral program operates
  and as needed for commission accounting.

---

## 7. Your rights

Under the GDPR and Argentine law 25.326 you may request:

- **access** to the personal data we hold about you;
- **rectification** of inaccurate data;
- **deletion** where there is no legal obligation to retain it;
- **portability** of data you provided;
- **objection** to processing based on legitimate interest;
- **restriction** of processing in certain cases.

To exercise any right, email **legal@vexinter.com**. We respond within
the timeframes required by applicable law. You may also lodge a
complaint with your local data-protection authority (in Argentina,
the AAIP).

---

## 8. Security

We apply reasonable technical and organizational measures: TLS in
transit, hashed credentials, signed license documents, access
controls on internal systems, and minimal data collection by design.
No method of transmission or storage is 100% secure; if a breach
affects your personal data we will notify you as required by law.

---

## 9. Changes

We may update this policy; the effective date above reflects the
current version. Material changes will be announced by email to
subscribers or by publishing the new version at
`vexinter.com/privacy` at least 30 days before they take effect.

---

## 10. Contact

**Facundo Oyola (Vexinter)** — Buenos Aires, Argentina
Privacy requests and questions: **legal@vexinter.com**
