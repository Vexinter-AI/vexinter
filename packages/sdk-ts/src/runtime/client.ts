/**
 * HTTP Client for Vexinter API
 * Handles signed requests, retries, and error mapping
 */

import { buildAuthHeaders, type RemoteCredentials } from '../identity';
import {
  canonicalJson,
  generateIdempotencyKey,
  RejectionError,
  RemoteError,
  type RejectionReason
} from '../crypto';

/** Actual shape the service returns for an invocation. */
export interface RemoteToolResult {
  ok: boolean;
  data?: unknown;
  error?: string | null;
  provenance: { tool: string; invoked_event: string; returned_event: string };
}

export type ToolFn = (args?: Record<string, unknown>) => Promise<RemoteToolResult>;
export type SystemNamespace = Record<string, ToolFn>;
export type ToolNamespace = Record<string, SystemNamespace>;

/** Transport retries (adjustable in tests). */
export const retryConfig = { retries: 3, baseMs: 1000, maxMs: 5000 };
const DEFAULT_TIMEOUT = 30000;

export interface HttpClientConfig {
  endpoint: string;
  credentials?: RemoteCredentials;
  adminToken?: string;
  /** Per-attempt timeout in ms (default 30000). */
  timeout?: number;
  /**
   * Total budget across all retry attempts in ms. Without it, retries can
   * stretch a call to ~(retries + 1) × timeout + backoff. When set, each
   * attempt gets at most the remaining budget and the call fails with
   * 504 once it is exhausted.
   */
  totalTimeout?: number;
}

/** undefined is not canonical JSON: it is sent as null, same as Python. */
function withoutUndefined(v: unknown): unknown {
  if (v === undefined) return null;
  if (Array.isArray(v)) return v.map(withoutUndefined);
  if (v && typeof v === 'object' && !(v instanceof Date)) {
    return Object.fromEntries(
      Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, withoutUndefined(x)]));
  }
  return v;
}

const isAbort = (e: unknown) => (e as { name?: string } | null)?.name === 'AbortError';

export class AsyncHttpClient {
  private readonly baseUrl: string;
  private readonly credentials?: RemoteCredentials;
  private readonly adminToken?: string;
  private readonly timeout: number;
  private readonly totalTimeout?: number;

  constructor(config: HttpClientConfig) {
    this.baseUrl = config.endpoint.replace(/\/+$/, '');
    this.credentials = config.credentials;
    this.adminToken = config.adminToken;
    this.timeout = config.timeout ?? DEFAULT_TIMEOUT;
    this.totalTimeout = config.totalTimeout;
  }

  /**
   * Signed request. GETs carry no body. Only GETs and calls with
   * `retry: true` are retried (invocations, which carry an idempotency
   * key): retrying any POST could duplicate an effect.
   */
  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options: {
      credentials?: RemoteCredentials;
      admin?: boolean;
      params?: Record<string, string>;
      retry?: boolean;
    } = {}
  ): Promise<T> {
    const creds = options.credentials ?? this.credentials;
    const isAdmin = Boolean(options.admin && this.adminToken);
    const verb = method.toUpperCase();
    const noBody = verb === 'GET' || verb === 'HEAD' || body === undefined;
    const bodyBytes = noBody
      ? new Uint8Array(0)
      : new TextEncoder().encode(canonicalJson(withoutUndefined(body)));

    const url = new URL(`${this.baseUrl}${path}`);
    if (options.params) {
      for (const [key, value] of Object.entries(options.params)) {
        url.searchParams.set(key, value);
      }
    }

    const retries = (options.retry || verb === 'GET') ? retryConfig.retries : 0;
    let ultimo: Error | null = null;
    const deadline = this.totalTimeout !== undefined
      ? Date.now() + this.totalTimeout
      : undefined;

    for (let attempt = 0; attempt <= retries; attempt++) {
      if (deadline !== undefined && Date.now() >= deadline) {
        throw new RemoteError(504, 'Request timeout (total budget exhausted)');
      }
      // Each attempt is signed again: with the same sequence the server would
      // reject it as a replay (§9.4).
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (creds && !isAdmin) {
        Object.assign(headers, buildAuthHeaders(method, path, bodyBytes, creds));
      }
      if (isAdmin) headers['X-Vex-Admin'] = this.adminToken!;

      const controller = new AbortController();
      const attemptBudget = deadline !== undefined
        ? Math.max(1, Math.min(this.timeout, deadline - Date.now()))
        : this.timeout;
      const temporizador = setTimeout(() => controller.abort(), attemptBudget);
      try {
        const response = await fetch(url.toString(), {
          method,
          headers,
          body: bodyBytes.length > 0 ? bodyBytes : undefined,
          signal: controller.signal
        });

        if (response.status >= 400) {
          const texto = await response.text();
          let cuerpo: unknown;
          try { cuerpo = JSON.parse(texto); } catch { cuerpo = texto; }
          // FastAPI wraps the error in { detail: ... }
          const detail = (cuerpo && typeof cuerpo === 'object' && 'detail' in (cuerpo as object))
            ? (cuerpo as { detail: unknown }).detail
            : cuerpo;
          if (response.status === 422 && detail && typeof detail === 'object'
              && 'reason' in (detail as object)) {
            const rej = detail as { reason: RejectionReason; detail?: string };
            throw new RejectionError(rej.reason, rej.detail ?? 'Rejected by server', 422, detail);
          }
          throw new RemoteError(response.status, detail);
        }

        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
          return undefined as T;
        }
        return await response.json() as T;
      } catch (error) {
        if (error instanceof RejectionError || error instanceof RemoteError) throw error;
        ultimo = error as Error;
        if (attempt === retries) {
          if (isAbort(error)) throw new RemoteError(504, 'Request timeout');
          throw new RemoteError(502, `Transport error after ${attempt + 1} attempt(s): ${ultimo.message}`);
        }
        await new Promise((r) => setTimeout(r, Math.min(retryConfig.baseMs * 2 ** attempt, retryConfig.maxMs)));
      } finally {
        clearTimeout(temporizador);
      }
    }
    throw ultimo!;
  }

  async close(): Promise<void> {
    // fetch needs no explicit cleanup
  }
}

/**
 * Invokes a tool with an idempotency key: the client generates one per
 * logical call and, if the response is lost, retries with the same one.
 */
export async function invokeTool(
  http: AsyncHttpClient,
  taskId: string,
  tool: string,
  args: Record<string, unknown>,
  credentials?: RemoteCredentials
): Promise<RemoteToolResult> {
  const body = { tool, args, idempotency_key: generateIdempotencyKey() };
  return http.request<RemoteToolResult>('POST', `/v1/tasks/${taskId}/invoke`, body,
    { credentials, retry: true });
}

/**
 * Dynamic namespace: task.tools.crm.create_customer({...}).
 * JavaScript has no __getattr__: it is resolved with a Proxy.
 */
export function makeToolNamespace(
  http: AsyncHttpClient,
  taskId: string,
  credentials?: RemoteCredentials
): ToolNamespace {
  return new Proxy({} as ToolNamespace, {
    get: (_t, system) => {
      if (typeof system !== 'string' || system === 'then') return undefined;
      return new Proxy({} as SystemNamespace, {
        get: (_s, tool) => {
          if (typeof tool !== 'string' || tool === 'then') return undefined;
          return (args?: Record<string, unknown>) =>
            invokeTool(http, taskId, `${system}.${tool}`, args ?? {}, credentials);
        }
      });
    }
  });
}
