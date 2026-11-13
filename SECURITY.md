# Security Policy

## Supported Versions

Vexinter is pre-1.0 software released under the Fair Source License (see
`LICENSE-COMMUNITY.md`). Security fixes are applied to the latest release on
the default branch.

| Version | Supported          |
| ------- | ------------------ |
| 0.5.x   | :white_check_mark: |
| < 0.5   | :x:                |

## Reporting a Vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Report privately via either channel:

- **GitHub Private Vulnerability Reporting**: Security tab → "Report a
  vulnerability" on `https://github.com/Vexinter-AI/vexinter` (preferred —
  encrypted, tracked, no email setup required).
- **Email**: `security@vexinter.com` with the subject `[SECURITY]`.

Include, when possible:

- A description of the vulnerability and its impact.
- Steps to reproduce or a proof of concept.
- The affected component (core, service, SDKs, console, deployment).
- Whether the issue requires authentication or specific configuration.

## What to Expect

- **Acknowledgement** within 72 hours.
- A status update within 7 days, including whether the report is accepted,
  needs more information, or is out of scope.
- If accepted, a fix or mitigation will be released as soon as reasonably
  possible, and you will be credited in the release notes unless you prefer to
  remain anonymous.

We ask that you give us reasonable time to fix the issue before public
disclosure. If a coordinated disclosure timeline is needed, we are happy to
agree on one.

## Scope

In scope:

- The Python core (`vexinter/`), the FastAPI service (`services/`), the
  operator console, and the TypeScript SDK (`packages/sdk-ts/`).
- Cryptographic issues: signature verification, envelope encryption,
  credential issuance/revocation, hash-chain integrity.
- Authentication and authorization bypasses (admin plane, agent credentials,
  SPA session, multi-tenancy enforcement).
- Injection, SSRF, path traversal, and similar classes in the service or
  console.
- Secrets exposure in the repository or in logs/metrics output.

Out of scope:

- Vulnerabilities in third-party dependencies, report those upstream; we will
  bump versions promptly once a fix is available.
- Issues that require the victim to run with `VEXINTER_DEV_MODE=1` or with the
  authentication plane deliberately disabled (documented insecure
  configurations).
- Denial of service against a self-hosted instance operated by the reporter.
- Social engineering, physical attacks, or compromised developer machines.

## Deployment Notes

Vexinter is designed to be self-hosted. Several security properties depend on
the operator's configuration:

- Terminate TLS in front of the service and set `VEXINTER_COOKIE_SECURE=1`.
- Never expose the administration plane without `VEXINTER_ADMIN_TOKEN`.
- Keep `VEXINTER_DEV_MODE` unset in production.
- The tenant/membership store is currently process-local; do not rely on it as
  the sole isolation boundary in horizontally scaled deployments.

Reports that only affect insecure operator configurations may still be
valuable, please note the required misconfiguration explicitly.
