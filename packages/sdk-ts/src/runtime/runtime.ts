/**
 * AsyncRemoteRuntime - Main entry point for Vexinter TypeScript SDK
 * Provides both data plane (tasks) and control plane (admin) operations
 */

import { AsyncHttpClient } from './client';
import { AsyncRemoteTask } from './task';
import { RemoteCredentials, createEnvelope, verifyAndDecryptEnvelope } from '../identity';
import type {
  AgentKeyMaterial,
  Credential,
  Envelope,
  Agent,
  Instance,
  RecoveryCase,
  VerdictResult,
  MetricsSnapshot,
  ChainAudit,
  TaskState,
  EraseBody,
  EraseResult,
  CaseDetail,
  CasePreview,
  AdminSummary,
  ChainVerifyResult
} from '../types';
import { VexinterError, vexCrypto, fromBase64 } from '../crypto';

export interface AsyncRemoteRuntimeConfig {
  endpoint: string;
  credentials?: RemoteCredentials;
  adminToken?: string;
  timeout?: number;
}

export class AsyncRemoteRuntime {
  public readonly http: AsyncHttpClient;
  public readonly endpoint: string;

  constructor(config: AsyncRemoteRuntimeConfig) {
    this.endpoint = config.endpoint;
    this.http = new AsyncHttpClient({
      endpoint: config.endpoint,
      credentials: config.credentials,
      adminToken: config.adminToken,
      timeout: config.timeout
    });
  }

  /**
   * Enter async context manager
   */
  async enter(): Promise<this> {
    return this;
  }

  /**
   * Exit async context manager
   */
  async exit(): Promise<void> {
    await this.close();
  }

  /**
   * Close the runtime and underlying HTTP client
   */
  async close(): Promise<void> {
    await this.http.close();
  }

  /**
   * Start a new supervised task
   */
  task(options: {
    agent: string;
    objective: string;
    condition: unknown;
    input?: Record<string, unknown>;
    snapshot?: unknown[];
    credentials?: RemoteCredentials;
  }): AsyncRemoteTask {
    return new AsyncRemoteTask(this, options);
  }

  // ============================================
  // CONTROL PLANE (Admin) - Identity Management
  // ============================================

  /**
   * Enroll a new agent
   */
  async enrollAgent(
    agentId: string,
    options: {
      displayName?: string;
      allowedTools?: string[];
      allowedRecipients?: string[];
    } = {}
  ): Promise<Agent> {
    return this.http.request<Agent>('POST', '/v1/identity/agents', {
      agent_id: agentId,
      display_name: options.displayName ?? '',
      allowed_tools: options.allowedTools?.sort() ?? null,
      allowed_recipients: options.allowedRecipients?.sort() ?? null
    }, { admin: true });
  }

  /**
   * Spawn a new instance for an agent
   */
  async spawnInstance(agentId: string): Promise<Instance> {
    return this.http.request<Instance>('POST', '/v1/identity/instances', {
      agent_id: agentId
    }, { admin: true });
  }

  /**
   * Issue a new credential for an instance
   */
  async issueCredential(instanceId: string, keys: AgentKeyMaterial): Promise<RemoteCredentials> {
    const response = await this.http.request<{
      agent_id: string;
      instance_id: string;
      credential_id: string;
    }>('POST', '/v1/identity/credentials', {
      instance_id: instanceId,
      signing_pub: RemoteCredentials.toBase64(keys.signing_pub),
      encryption_pub: RemoteCredentials.toBase64(keys.encryption_pub)
    }, { admin: true });

    return new RemoteCredentials(
      response.agent_id,
      response.instance_id,
      response.credential_id,
      keys
    );
  }

  /**
   * Rotate credentials (create successor keys)
   */
  async rotateCredentials(
    credentials?: RemoteCredentials,
    retainSeconds: number = 900
  ): Promise<RemoteCredentials> {
    const creds = credentials ?? this.http['credentials'];
    if (!creds) {
      throw new VexinterError('No credentials available for rotation', 400, 'NO_CREDENTIALS');
    }

    // Generate successor keys locally
    const { signing, encryption } = await vexCrypto.deriveSuccessorKeyPair(
      creds.keys.signing_priv,
      creds.keys.encryption_priv
    );

    const newKeys: AgentKeyMaterial = {
      signing_priv: signing.privateKey,
      signing_pub: signing.publicKey,
      encryption_priv: encryption.privateKey,
      encryption_pub: encryption.publicKey
    };

    const response = await this.http.request<{
      agent_id: string;
      instance_id: string;
      credential_id: string;
      previous_valid_until?: string;
    }>('POST', '/v1/identity/credentials/rotate', {
      signing_pub: RemoteCredentials.toBase64(newKeys.signing_pub),
      encryption_pub: RemoteCredentials.toBase64(newKeys.encryption_pub)
    }, { credentials: creds });

    // The rotated-out credential stays valid until previous_valid_until
    // (min of its expires_at and grace end). Stamp it so local expiry
    // checks stop accepting it once grace closes.
    if (response.previous_valid_until) {
      creds.keys.grace_until = response.previous_valid_until;
    }

    return new RemoteCredentials(
      response.agent_id,
      response.instance_id,
      response.credential_id,
      newKeys
    );
  }

  /**
   * Revoke a credential
   */
  async revokeCredential(credentialId: string, reason: string = ''): Promise<void> {
    await this.http.request('DELETE', `/v1/identity/credentials/${credentialId}`, undefined, {
      admin: true,
      params: { reason }
    });
  }

  /**
   * List all agents
   */
  async listAgents(): Promise<Agent[]> {
    return this.http.request<Agent[]>('GET', '/v1/identity/agents', undefined, { admin: true });
  }

  /**
   * Get pending recovery cases
   */
  async pendingCases(): Promise<RecoveryCase[]> {
    return this.http.request<RecoveryCase[]>('GET', '/v1/cases', undefined, { admin: true });
  }

  /**
   * Decide on a recovery case
   */
  async decide(caseId: string, action: 'retry' | 'reverify' | 'compensate' | 'close', operator: string = 'operator'): Promise<VerdictResult | unknown> {
    return this.http.request('POST', `/v1/cases/${caseId}/decide`, {
      action,
      operator
    }, { admin: true });
  }

  /**
   * Run due followups
   */
  async runDueFollowups(): Promise<unknown> {
    return this.http.request('POST', '/v1/followups/run-due', undefined, { admin: true });
  }

  /**
   * Get metrics snapshot
   */
  async metrics(): Promise<MetricsSnapshot> {
    return this.http.request<MetricsSnapshot>('GET', '/v1/metrics', undefined, { admin: true });
  }

  /**
   * Take a checkpoint
   */
  async takeCheckpoint(): Promise<ChainAudit> {
    return this.http.request<ChainAudit>('POST', '/v1/checkpoints', undefined, { admin: true });
  }

  /**
   * Get admin task details
   */
  async adminTask(taskId: string): Promise<TaskState> {
    return this.http.request<TaskState>('GET', `/v1/admin/tasks/${taskId}`, undefined, { admin: true });
  }

  /**
   * Get encryption public key for an agent
   */
  async getEncryptionPub(agentId: string): Promise<string> {
    const response = await this.http.request<{ encryption_pub: string }>(
      'GET',
      '/v1/identity/key',
      undefined,
      { params: { agent: agentId } }
    );
    return response.encryption_pub;
  }

  /**
   * Get next message sequence number
   */
  async getMessageSequence(fromAgent: string, toAgent: string): Promise<number> {
    const response = await this.http.request<{ next: number }>(
      'GET',
      '/v1/messages/sequence',
      undefined,
      { params: { from_agent: fromAgent, to_agent: toAgent } }
    );
    return response.next;
  }

  /**
   * Send a message to another agent
   * §9.5: Creates envelope with E2E encryption and sends to /v1/messages
   */
  async sendMessage(
    toAgent: string,
    payload: unknown,
    options: {
      credentials?: RemoteCredentials;
      taskId?: string;
      executionId?: string;
    } = {}
  ): Promise<void> {
    const creds = options.credentials ?? this.http['credentials'];
    if (!creds) {
      throw new VexinterError('No credentials available for sending message', 400, 'NO_CREDENTIALS');
    }

    // Serialize per (from_agent → to_agent): the sequence must be fetched
    // and consumed atomically or two concurrent sends get the same next
    // value and the second is rejected as a replay.
    const pairKey = `${creds.agent_id}->${toAgent}`;
    const prev = this._sendLocks.get(pairKey) ?? Promise.resolve();
    const run = prev.then(() => this._sendMessageLocked(
      toAgent, payload, creds, options
    ));
    this._sendLocks.set(pairKey, run.then(() => undefined, () => undefined));
    await run;
  }

  private _sendLocks = new Map<string, Promise<void>>();

  private async _sendMessageLocked(
    toAgent: string,
    payload: unknown,
    creds: RemoteCredentials,
    options: { taskId?: string; executionId?: string }
  ): Promise<void> {
    // Get recipient's encryption public key
    const encPubB64 = await this.getEncryptionPub(toAgent);
    const toEncryptionPub = fromBase64(encPubB64);

    // Strictly increasing sequence per pair (from_agent → to_agent);
    // the service requires it in §9.4. It must be fetched BEFORE signing.
    const sequence = await this.getMessageSequence(creds.agent_id, toAgent);

    // Create envelope with E2E encryption (taskId/executionId go into the
    // AAD and the signature: they cannot be modified afterwards)
    const envelope = await createEnvelope(creds, toAgent, toEncryptionPub, payload, {
      taskId: options.taskId ?? null,
      executionId: options.executionId ?? null,
      sequence
    });

    // Send to service
    await this.http.request('POST', '/v1/messages', {
      envelope: envelope
    }, { credentials: creds });
  }

  /**
   * Get inbox for credentials
   */
  async inbox(credentials?: RemoteCredentials): Promise<Envelope[]> {
    return this.http.request<Envelope[]>('GET', '/v1/messages/inbox', undefined, { credentials });
  }

  /**
   * Receive and verify all messages for an agent
   * §9.5: Verifies signature, decrypts E2E, verifies body_hash, acknowledges
   */
  async receiveAll(
    keys: AgentKeyMaterial,
    credentials?: RemoteCredentials
  ): Promise<unknown[]> {
    const creds = credentials ?? this.http['credentials'];
    const envelopes = await this.inbox(creds);
    const results: unknown[] = [];

    // Binding target: explicit credentials win, otherwise the key material's
    // agent_id — never skip the check when creds is undefined.
    const boundAgentId = creds?.agent_id ?? keys.agent_id;

    // Cache for sender credentials
    const senderCredsCache = new Map<string, Credential>();

    for (const envelope of envelopes) {
      // Verify this envelope is for us
      if (boundAgentId && envelope.header.to_agent !== boundAgentId) {
        throw new VexinterError('Envelope not addressed to this agent', 422, 'BINDING_MISMATCH');
      }

      // Get sender's public credential
      const cid = envelope.header.from_credential;
      let senderCred = senderCredsCache.get(cid);
      if (!senderCred) {
        senderCred = await this.http.request<Credential>(
          'GET',
          `/v1/identity/credentials/${cid}/public`,
          undefined,
          { credentials }
        );
        senderCredsCache.set(cid, senderCred);
      }

      // Verify and decrypt
      const plaintext = await verifyAndDecryptEnvelope(envelope, keys, senderCred);

      // Acknowledge receipt
      await this.http.request('POST', '/v1/messages/ack', {
        message_id: envelope.header.message_id,
        body_hash: envelope.body_hash
      }, { credentials: creds });

      results.push(plaintext);
    }

    return results;
  }

  /**
   * Get admin summary
   */
  async adminSummary(): Promise<AdminSummary> {
    return this.http.request<AdminSummary>('GET', '/v1/admin/summary', undefined, { admin: true });
  }

  /**
   * Privacy erase - cryptographic erasure of executions (§16)
   * Destroys encryption keys for the given executions, making personal content unreadable.
   * Requires admin token. Only works on completed executions with no pending cases.
   */
  async privacyErase(executionIds: string[], reason: string = ''): Promise<EraseResult> {
    const body: EraseBody = { execution_ids: executionIds, reason };
    return this.http.request<EraseResult>('POST', '/v1/privacy/erase', body, { admin: true });
  }

  /**
   * Get case detail with options and context
   */
  async caseDetail(caseId: string): Promise<CaseDetail> {
    return this.http.request<CaseDetail>('GET', `/v1/cases/${caseId}`, undefined, { admin: true });
  }

  /**
   * Get case preview - shows what would happen with each recovery action
   */
  async casePreview(caseId: string): Promise<CasePreview> {
    return this.http.request<CasePreview>('POST', `/v1/cases/${caseId}/preview`, undefined, { admin: true });
  }

  /**
   * Verify chain integrity for a stream
   */
  async verifyChain(stream?: string): Promise<ChainVerifyResult> {
    const params: Record<string, string> = {};
    if (stream) params.stream = stream;
    return this.http.request<ChainVerifyResult>('GET', '/v1/chains/verify', undefined, { admin: true, params });
  }

  // Helper methods (private)

  private async getCredential(credentialId: string): Promise<Credential> {
    return this.http.request<Credential>('GET', `/v1/identity/credentials/${credentialId}/public`);
  }
}