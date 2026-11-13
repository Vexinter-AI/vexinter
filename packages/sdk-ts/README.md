# @vexinter-ai/sdk

TypeScript/JavaScript SDK for [Vexinter](https://vexinter.com); verifiable, recoverable, secure AI agents.

Same cryptographic protocol as the Python SDK and service: Ed25519 signatures, X25519 ECDH, HKDF-SHA256 and AES-GCM envelopes, fully interoperable in both directions (verified by cross-language test vectors).

## Install

```bash
npm install @vexinter-ai/sdk
```

Requires Node.js >= 20.

## Quick start

```ts
import { AsyncRemoteRuntime, createAgentKeyMaterial } from "@vexinter-ai/sdk";

// Admin plane: enroll agent, spawn instance, issue credential
// (private Ed25519/X25519 keys are generated locally, never leave Node)
const adm = new AsyncRemoteRuntime({
  endpoint: "https://api.tudominio.com",
  adminToken: process.env.VEXINTER_ADMIN_TOKEN!,
});

await adm.enrollAgent("bot-ts", {
  allowedTools: ["crm.create_customer", "billing.create_invoice"],
});
const inst = await adm.spawnInstance("bot-ts");
const creds = await adm.issueCredential(
  inst.instance_id,
  createAgentKeyMaterial(),
);

// Data plane: signed, supervised task with declarative condition
const rt = new AsyncRemoteRuntime({
  endpoint: "https://api.tudominio.com",
  credentials: creds,
});

const task = await rt
  .task({
    agent: "bot-ts",
    objective: "Alta de cliente y factura",
    condition: {
      op: "equals",
      a: { from: "crm", path: "status" },
      b: "active",
    },
    input: { email: "cliente@ejemplo.com" },
    credentials: creds,
  })
  .enter();

const verdict = await task.verify();
console.log("Estado de cumplimiento:", verdict.status);
```

## Encrypted envelopes

`createEnvelope` / `verifyAndDecryptEnvelope` implement the same wire format as `vexinter.identity.envelope` in Python, envelopes produced by either SDK validate and decrypt on the other side.

## License

[Vexinter Community License](./LICENSE.txt) (source-available, non-commercial; converts to MIT two years after each version's publication), free for development, evaluation and non-commercial use. Commercial use requires a [Vexinter subscription](https://vexinter.com/pricing).

## Links

- Website: https://vexinter.com
- Repo: https://github.com/Vexinter-AI/vexinter
- Python SDK: `pip install vexinter` (see [README](https://github.com/Vexinter-AI/vexinter#readme))
