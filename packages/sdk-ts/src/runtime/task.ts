/**
 * AsyncRemoteTask - Main task interface for Vexinter TypeScript SDK
 * Mirrors the Python AsyncRemoteTask API
 */

import { AsyncHttpClient, invokeTool, makeToolNamespace } from './client';
import type { RemoteToolResult, ToolNamespace } from './client';
import type {
  VerdictResult,
  RecoveryCase,
  CaseOption,
  Plan,
  ExecutionState,
  Event,
  ChainAudit,
  MemoryEpisode,
  MemoryBlock,
  MemoryQuery,
  TaskState,
  ExecutionGraph
} from '../types';
import { RemoteCredentials } from '../identity';
import { VexinterError } from '../crypto';
import { memoryPromptBlock } from './memory';
import { AsyncRemoteRuntime } from './runtime';

export interface AsyncRemoteTaskOptions {
  agent: string;
  objective: string;
  condition: unknown;
  input?: Record<string, unknown>;
  snapshot?: unknown[];
  credentials?: RemoteCredentials;
  /** Reuse across retries of the same logical task creation: the
   *  service returns the original task instead of charging quota twice. */
  idempotencyKey?: string;
  /** Per-task budget/guardrail overrides sent to POST /v1/tasks:
   *  max_tokens, max_usd, model_pricing, max_steps, deadline_s,
   *  loop_threshold, cycle_threshold, no_progress_window. */
  budget?: Record<string, unknown>;
}

export class AsyncRemoteTask {
  private readonly runtime: AsyncRemoteRuntime;
  public readonly agent: string;
  public readonly objective: string;
  public readonly condition: unknown;
  public readonly input: Record<string, unknown>;
  public readonly snapshot: unknown[];

  private _taskId: string | null = null;
  private _executionId: string | null = null;
  private _credentials?: RemoteCredentials;
  private _lastVerdict?: VerdictResult;
  private _closed = false;
  private readonly _idempotencyKey?: string;
  private readonly _budget?: Record<string, unknown>;

  constructor(runtime: AsyncRemoteRuntime, options: AsyncRemoteTaskOptions) {
    this.runtime = runtime;
    this.agent = options.agent;
    this.objective = options.objective;
    this.condition = options.condition;
    this.input = options.input ?? {};
    this.snapshot = options.snapshot ?? [];
    this._credentials = options.credentials;
    this._idempotencyKey = options.idempotencyKey;
    this._budget = options.budget;
  }

  /**
   * Enter the async context manager - starts the task
   */
  async enter(): Promise<this> {
    const response = await this.runtime.http.request<{ task_id: string; execution_id: string }>(
      'POST',
      '/v1/tasks',
      {
        agent: this.agent,
        objective: this.objective,
        condition: this.condition,
        input: this.input,
        snapshot: this.snapshot,
        ...(this._idempotencyKey ? { idempotency_key: this._idempotencyKey } : {}),
        ...(this._budget ?? {})
      },
      { credentials: this._credentials }
    );

    this._taskId = response.task_id;
    this._executionId = response.execution_id;
    return this;
  }

  /**
   * Exit the async context manager - completes the task
   */
  async exit(error?: Error): Promise<void> {
    if (this._closed || !this._taskId) return;

    try {
      await this.runtime.http.request(
        'POST',
        `/v1/tasks/${this._taskId}/complete`,
        {
          exception: error !== undefined,
          last_status: this._lastVerdict?.status ?? null
        },
        { credentials: this._credentials }
      );
    } finally {
      this._closed = true;
    }
  }

  // Context manager protocol for JavaScript/TypeScript (using [Symbol.asyncDispose])
  async [Symbol.asyncDispose](): Promise<void> {
    await this.exit();
  }

  get taskId(): string | null {
    return this._taskId;
  }

  get executionId(): string | null {
    return this._executionId;
  }

  get credentials(): RemoteCredentials | undefined {
    return this._credentials;
  }

  /**
   * Access tools namespace (dynamic: t.tools.crm.create_customer)
   */
  get tools(): ToolNamespace {
    this._requireOpen();
    return makeToolNamespace(this.runtime.http, this._taskId!, this._credentials);
  }

  private _collaborators = new Set<string>();

  /**
   * Add a collaborator to this task
   */
  async addCollaborator(agent: string): Promise<void> {
    this._requireOpen();
    await this.runtime.http.request(
      'POST',
      `/v1/tasks/${this._taskId}/collaborators`,
      { agent },
      { credentials: this._credentials }
    );
  }

  /**
   * Call a tool as another agent (with their credentials)
   */
  async callAs(credentials: RemoteCredentials, tool: string, args: Record<string, unknown>): Promise<RemoteToolResult> {
    this._requireOpen();

    // Check if we need to add as collaborator (once per agent per task)
    const owner = this._credentials?.agent_id ?? this.agent;
    if (credentials.agent_id !== owner && !this._collaborators.has(credentials.agent_id)) {
      try {
        await this.addCollaborator(credentials.agent_id);
      } catch (e) {
        // Already a collaborator (e.g. added in a previous session): 409 is fine.
        if (!(e instanceof Error && 'statusCode' in e && (e as {statusCode?: number}).statusCode === 409)) {
          throw e;
        }
      }
      this._collaborators.add(credentials.agent_id);
    }

    return invokeTool(this.runtime.http, this._taskId!, tool, args, credentials);
  }

  /**
   * Verify the task against its condition
   */
  async verify(): Promise<VerdictResult> {
    this._requireOpen();
    const verdict = await this.runtime.http.request<VerdictResult>(
      'POST',
      `/v1/tasks/${this._taskId}/verify`,
      {},
      { credentials: this._credentials }
    );
    this._lastVerdict = verdict;
    return verdict;
  }

  /**
   * Request recovery for a failed tool
   */
  async recover(failedTool?: string, invokedEvent?: string): Promise<RecoveryCase | null> {
    this._requireOpen();
    const response = await this.runtime.http.request<{ case?: RecoveryCase }>(
      'POST',
      `/v1/tasks/${this._taskId}/recover`,
      { failed_tool: failedTool, invoked_event: invokedEvent },
      { credentials: this._credentials }
    );
    return response.case ?? null;
  }

  /**
   * Run a plan on this task
   */
  async runPlan(plan: Plan): Promise<VerdictResult | null> {
    this._requireOpen();
    const response = await this.runtime.http.request<{ verdict?: VerdictResult }>(
      'POST',
      `/v1/tasks/${this._taskId}/plan`,
      { plan },
      { credentials: this._credentials }
    );
    return response.verdict ?? null;
  }

  /**
   * Run due followups for this task
   */
  async runFollowups(): Promise<unknown> {
    return this.runtime.runDueFollowups();
  }

  /**
   * Get current execution state
   */
  async getState(): Promise<ExecutionState> {
    this._requireOpen();
    const response = await this.runtime.http.request<{ state: ExecutionState }>(
      'GET',
      `/v1/tasks/${this._taskId}`,
      {},
      { credentials: this._credentials }
    );
    return response.state;
  }

  /**
   * Get all events for this task
   */
  async events(): Promise<Event[]> {
    this._requireOpen();
    return this.runtime.http.request<Event[]>(
      'GET',
      `/v1/tasks/${this._taskId}/events`,
      {},
      { credentials: this._credentials }
    );
  }

  /**
   * Get execution graph
   */
  async graph(): Promise<ExecutionGraph> {
    this._requireOpen();
    return this.runtime.http.request<ExecutionGraph>(
      'GET',
      `/v1/tasks/${this._taskId}/graph`,
      {},
      { credentials: this._credentials }
    );
  }

  /**
   * Get chain audit for this task
   */
  async audit(): Promise<ChainAudit> {
    this._requireOpen();
    return this.runtime.http.request<ChainAudit>(
      'GET',
      `/v1/tasks/${this._taskId}/audit`,
      {},
      { credentials: this._credentials }
    );
  }

  /**
   * Query memory
   */
  async memory(query: MemoryQuery = {}): Promise<MemoryEpisode[]> {
    const params: Record<string, string> = {};
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) {
        params[key] = Array.isArray(value) ? value.join(',') : String(value);
      }
    }
    const response = await this.runtime.http.request<{ results: MemoryEpisode[] }>(
      'GET',
      '/v1/memory',
      {},
      { credentials: this._credentials, params }
    );
    return response.results;
  }

  /**
   * Get formatted memory block for prompt injection
   */
  async memoryBlock(query: MemoryQuery = {}): Promise<MemoryBlock> {
    const episodes = await this.memory(query);
    // Los recuerdos van como DATOS, con el mismo recorte y saneado que en Python.
    const block = memoryPromptBlock(episodes as unknown as Array<Record<string, unknown>>);
    return { block, episodes };
  }

  /**
   * Get pending recovery cases for this task
   */
  async pendingCases(): Promise<RecoveryCase[]> {
    // El servicio devuelve [{ case, options }], no una lista de casos planos.
    const filas = (await this.runtime.pendingCases()) as unknown as Array<{ case: Record<string, unknown>; options: unknown[] }>;
    return filas.map(f => f.case).filter(c => c.task_id === this._taskId) as unknown as RecoveryCase[];
  }

  /**
   * Get options for a specific case
   */
  async caseOptions(caseId: string): Promise<CaseOption[]> {
    const filas = (await this.runtime.pendingCases()) as unknown as Array<{ case: Record<string, unknown>; options: unknown[] }>;
    const fila = filas.find(f => f.case.id === caseId);
    return (fila?.options ?? []) as CaseOption[];
  }

  /**
   * Decide on a recovery case
   */
  async decide(caseId: string, action: 'retry' | 'reverify' | 'compensate' | 'close', operator: string = 'operator'): Promise<VerdictResult | unknown> {
    const response = await this.runtime.decide(caseId, action, operator);
    if (response && typeof response === 'object' && 'status' in response) {
      return response as VerdictResult;
    }
    return response;
  }

  private _requireOpen(): void {
    if (!this._taskId) {
      throw new VexinterError('Task not started. Use "await task.enter()" or "using task = await runtime.task(...)"', 500, 'TASK_NOT_STARTED');
    }
    if (this._closed) {
      throw new VexinterError('Task already closed', 500, 'TASK_CLOSED');
    }
  }
}