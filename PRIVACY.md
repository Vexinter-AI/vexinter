# Privacy Policy

**Effective date:** November 30, 2026
**Controller:** Facundo Oyola (Vexinter), Argentina
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
correspondence. We do not process special-category (sensitive) data
such as health, biometric or similar.

---

## 2. What we process and why

### 2.1 Billing and subscription data

Purchases are processed by **Polar** as Merchant of Record. Polar
collects and processes your payment details (card data, billing
address, tax identifiers) under its own privacy policy. We receive
from Polar: your email, name, plan, subscription status and order
identifiers. We use this to:

- deliver the license and package access you purchased
  (**contract**);
- issue invoices and comply with tax obligations (**legal
  obligation**);
- provide support and enforce the Terms of Service (**legitimate
  interest** in operating the service and preventing abuse).

### 2.2 License enforcement data

A licensed instance fetches its signed license document from
`billing.vexinter.com` approximately once per day of active use. Each
request carries your **license token** (a `Bearer` credential issued
for your customer id) and necessarily exposes your **IP address** and
**customer id (`lic_…`)** to our billing infrastructure. They are used
for license enforcement, security and abuse prevention only — never
for usage profiling (**contract** and **legitimate interest** in
license enforcement).
Access logs are retained for up to **90 days** and then deleted.
Signed license documents are kept for the duration of the subscription
plus **10 years** for license enforcement, commission accounting and
tax/legal obligations (AFIP/ARCA).

### 2.3 Referral attribution

If you purchase after following a creator referral link, we store the
paid order's metadata (referral code, order/subscription identifiers,
plan, amount, currency, timestamps) to calculate commissions
(**legitimate interest** in operating the referral program and
**contract** with the referring creator). No additional personal data
is collected for this purpose.

### 2.4 Creator and referral partner data

Commissions to creators are paid **directly by us** (not through
Polar). To operate the program we process the personal data creators
provide for payouts and program administration: display name or
handle, contact email, tax identifiers where required, and payout
details (e.g. bank account or wallet address). We use this to:

- calculate and pay commissions (**contract** with the creator);
- issue invoices and comply with tax withholding/reporting
  obligations (**legal obligation**);
- prevent fraud and enforce the agreement with the creator
  (**legitimate interest**).

Payout details are sent through the payout method chosen by the
creator (bank transfer, PayPal, Wise, crypto wallet, etc.).

### 2.5 Support and correspondence

When you contact us (support, sales, legal), we process the content
of your messages and your contact details to respond and to keep
records of the interaction (**legitimate interest** in customer
communication; **contract** where the inquiry precedes a purchase).

### 2.6 Self-hosted deployments

The Community and Commercial editions running on your infrastructure
process agent data, payloads and keys **on your systems**. We have no
access to them and do not receive telemetry from them. The only
outbound connection is the daily license check described in §2.2,
which carries no task or agent data.

### 2.7 Website

This website is a static site hosted on Cloudflare Pages. We do not
run analytics or tracking scripts. Cloudflare may process standard
technical request data (IP address, user agent) to deliver and protect
the site, under its own privacy policy (**legitimate interest** in
keeping the site available and secure).

---

## 3. Cookies

- **vexinter.com**: we do not use analytics or advertising cookies.
  The only cookie we set is **`vx_ref`**, a first-party attribution
  cookie that stores a creator referral code (not personal data by
  itself) for **90 days**. It is written only after the visitor
  accepts a notice shown when arriving through a referral link, is
  used solely to credit the referral if a purchase follows, and can
  be deleted at any time via the **Referral cookie preferences**
  section at the bottom of this page. It is never used for
  advertising, profiling, or shared with third parties.
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
| Resend | Transactional email (welcome, license delivery) | Email address, name |
| Backblaze B2 | Encrypted backup of billing and license records | Encrypted copies of signed license documents and referral records (client-side encrypted; the provider cannot read them) |
| Creator-selected payout rail | Sending commission payments | Payout details the creator provides (e.g. bank account or wallet address), name, amount |

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

- **Billing records**: retained for up to **10 years**, as required
  by Argentine tax and accounting law.
- **License-check access logs**: up to 90 days.
- **Signed license documents**: duration of the subscription plus 10
  years (see §2.2).
- **Support correspondence**: retained for the duration of the
  relationship plus **3 years** for legal claims.
- **Referral records**: retained for **10 years** for commission
  accounting and tax obligations (AFIP/ARCA).
- **Creator payout data**: retained for the duration of the
  relationship plus **10 years** for tax and accounting obligations
  (AFIP/ARCA).

---

## 7. Your rights

Under the GDPR and Argentine law 25.326 you may request:

- **access** to the personal data we hold about you;
- **rectification** of inaccurate data;
- **deletion** where there is no legal obligation to retain it;
- **portability** of data you provided;
- **objection** to processing based on legitimate interest;
- **restriction** of processing in certain cases.

To exercise any right, email **legal@vexinter.com** with your full
name, the data concerned and the right you wish to exercise. To
protect your data we verify your identity: for requests under
Argentine law we will ask for your identity document (DNI or
equivalent); for other requests we may ask for the information
needed to confirm it. We use this only to verify your request and
do not keep copies longer than necessary. We respond within the
timeframes required by applicable law. You may also lodge a
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

**Facundo Oyola (Vexinter)** — Argentina
Privacy requests and questions: **legal@vexinter.com**
