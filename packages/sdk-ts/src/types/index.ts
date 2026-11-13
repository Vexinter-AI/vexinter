/**
 * Core types for Vexinter TypeScript SDK
 */

// Generated types from OpenAPI spec
export * from './generated';

export type EventType =
  | 'TASK_CREATED'
  | 'TOOL_INVOKED'
  | 'TOOL_RETURNED'
  | 'TOOL_REPLAYED'
  | 'VERIFIER_VERDICT'
  | 'RECOVERY_CASE_CREATED'
  | 'RECOVERY_DECISION'
  | 'PRIVACY_ERASED'
  | 'MESSAGE_SENT'
  | 'MESSAGE_RECEIVED'
  | 'CHECKPOINT_TAKEN'
  | 'AGENT_ENROLLED'
  | 'CREDENTIAL_ISSUED'
  | 'CREDENTIAL_ROTATED'
  | 'CREDENTIAL_REVOKED'
  | 'INSTANCE_SPAWNED'
  | 'FOLLOWUP_SCHEDULED'
  | 'FOLLOWUP_EXECUTED';

export type ExecutionState =
  | 'in_progress'
  | 'pending_verification'
  | 'provisionally_satisfied'
  | 'satisfied'
  | 'failed'
  | 'inconclusive'
  | 'pending_recovery'
  | 'pending_human_decision';

export type VerdictStatus = 'met' | 'not_met' | 'inconclusive';

export type Consistency = 'strong' | 'eventual' | 'unknown';

export type RecoveryClass = 'READ' | 'IDEMPOTENT_WRITE' | 'REVERSIBLE_WRITE' | 'EFFECTFUL_IRREVERSIBLE';

export type RecoveryAction = 'retry' | 'reverify' | 'compensate' | 'close';

export type ToolApproval = 'auto' | 'human';

export interface EventDraft {
  type: EventType;
  payload?: Record<string, unknown>;
  task_id?: string;
  execution_id?: string;
  agent_id?: string;
  operation_id?: string;
  caused_by?: string;
  depends_on?: string[];
  compensates?: string;
}

export interface Event extends EventDraft {
  id: string;
  stream: string;
  seq: number;
  ts: string;
  prev_hash: string;
  this_hash: string;
  signature?: string;
  payload_hash: string;
  payload_ref?: string;
}

export interface EventQuery {
  task_id?: string;
  execution_id?: string;
  agent_id?: string;
  types?: EventType[];
  since?: string;
  until?: string;
  limit?: number;
}

export interface ChainAudit {
  ok: boolean;
  stream: string;
  events_checked: number;
  head_seq: number;
  head_hash: string;
  problems: Array<{
    seq: number;
    expected_hash: string;
    actual_hash: string;
    problem: string;
  }>;
}

export interface Checkpoint {
  id: string;
  ts: string;
  heads: Record<string, { seq: number; head_hash: string }>;
  signature?: string;
}

export interface VerdictResult {
  status: VerdictStatus;
  provisional: boolean;
  followup_required: boolean;
  checks: CheckResult[];
  evidence: EvidenceEntry[];
  scope: 'task' | 'post_recovery' | 'step_postcondition';
}

export interface CheckResult {
  node: string;
  status: VerdictStatus;
  reason?: string;
  consistency?: Consistency;
  checks?: CheckResult[];
}

export interface EvidenceEntry {
  system: string;
  source: string;
  consistency: Consistency;
  observed_at: string;
  observed_hash: string;
}

export interface RecoveryCase {
  id: string;
  execution_id: string;
  task_id: string;
  failed_tool?: string;
  invoked_event?: string;
  status: 'open' | 'resolved' | 'closed';
  created_at: string;
  options: CaseOption[];
  decided_at?: string;
  decided_by?: string;
  decision?: RecoveryAction;
}

export interface CaseOption {
  action: RecoveryAction;
  label: string;
  description: string;
  requires_approval: boolean;
  available: boolean;
}

export interface RemoteToolResult {
  ok: boolean;
  data?: unknown;
  error?: string | null;
  provenance: {
    tool: string;
    invoked_event: string;
    returned_event: string;
  };
  idempotency_key?: string;
}

export interface Provenance {
  tool: string;
  args: Record<string, unknown>;
  invocation_id: string;
  execution_id: string;
  ts: string;
  retried: boolean;
  replayed: boolean;
}

export interface TaskState {
  task_id: string;
  execution_id: string;
  state: ExecutionState;
  objective: string;
  condition: ConditionNode;
  agent: string;
  created_at: string;
  updated_at: string;
}

export interface ExecutionGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphNode {
  id: string;
  type: string;
  label: string;
  status: string;
  metadata?: Record<string, unknown>;
}

export interface GraphEdge {
  from: string;
  to: string;
  type: string;
}

export type ConditionOp =
  | 'equals'
  | 'not_equals'
  | 'contains'
  | 'not_contains'
  | 'greater_than'
  | 'less_than'
  | 'exists'
  | 'not_exists'
  | 'count'
  | 'all'
  | 'any'
  | 'not'
  | 'snapshot_changed'
  | 'within_window'
  | 'field_equals'
  | 'var_equals';

export interface ConditionNode {
  op: ConditionOp;
  a?: ConditionValue;
  b?: ConditionValue;
  args?: ConditionNode[];
  as?: string;
  source?: string;
  path?: string;
  window_seconds?: number;
  review_after_seconds?: number;
}

export type ConditionValue =
  | string
  | number
  | boolean
  | { from: 'input' | 'steps' | 'observe' | 'memory' | 'verifier'; path: string; as?: string }
  | { ref: string };

export interface MemoryQuery {
  agent_id?: string;
  task_id?: string;
  execution_id?: string;
  types?: string[];
  since?: string;
  until?: string;
  limit?: number;
  min_confidence?: number;
}

export interface MemoryEpisode {
  id: string;
  agent_id: string;
  task_id?: string;
  execution_id?: string;
  type: 'episodic' | 'semantic' | 'working';
  content: string;
  confidence: number;
  tags: string[];
  created_at: string;
  verified_at?: string;
  verdict_id?: string;
  status: 'proposed' | 'supported' | 'refuted';
}

export interface MemoryBlock {
  block: string;
  episodes: MemoryEpisode[];
}

export interface AgentKeyMaterial {
  signing_priv: Uint8Array;
  signing_pub: Uint8Array;
  encryption_priv: Uint8Array;
  encryption_pub: Uint8Array;
  credential_id?: string;
  instance_id?: string;
  agent_id?: string;
  expires_at?: string;
  /** Set on a rotated-out credential: effective end of validity is
   * min(expires_at, grace_until), same as Python's Credential.valid_until. */
  grace_until?: string;
}

export interface Credential {
  credential_id: string;
  agent_id: string;
  instance_id: string;
  signing_pub: Uint8Array;
  encryption_pub: Uint8Array;
  issued_at: string;
  expires_at: string;
  revoked_at?: string;
  revoked_reason?: string;
  successor_credential_id?: string;
  superseded_by?: string;
  grace_until?: string;
}

export interface Agent {
  agent_id: string;
  display_name: string;
  allowed_tools: string[];
  allowed_recipients: string[];
  created_at: string;
  updated_at: string;
}

export interface Instance {
  instance_id: string;
  agent_id: string;
  created_at: string;
  revoked_at?: string;
}

export interface Envelope {
  header: EnvelopeHeader;
  body_hash: string;
  eph_pub: string;
  nonce: string;
  ct: string;
  ct_hash: string;
  signature: string;
}

export interface EnvelopeHeader {
  message_id: string;
  from_agent: string;
  from_instance: string;
  from_credential: string;
  to_agent: string;
  task_id: string | null;
  execution_id: string | null;
  ts: string;
  sequence: number;
  encryption_pub: string;
}

export type RejectionReason =
  | 'invalid_credential'
  | 'binding_mismatch'
  | 'bad_signature'
  | 'tampering'
  | 'stale_timestamp'
  | 'replay'
  | 'not_authorized';

export interface Rejection {
  reason: RejectionReason;
  detail: string;
}

export interface MessageAck {
  message_id: string;
  body_hash: string;
  acked_at: string;
}

// Response types for API operations
export interface MessageSent {
  message_id: string;
  sequence: number;
}

export interface TaskCreatedResponse {
  task_id: string;
  execution_id: string;
}

export interface TaskSummary {
  task_id: string;
  execution_id: string;
  objective: string;
  state: ExecutionState;
  agent: string;
  created_at: string;
}

export interface TaskDetail extends TaskState {
  events: Event[];
  graph: ExecutionGraph;
}

export type ToolResult = RemoteToolResult;

export interface ApiResponse<T> {
  data: T;
  meta?: {
    request_id: string;
    timestamp: string;
  };
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export class VexinterError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number,
    public readonly code: string,
    public readonly details?: unknown
  ) {
    super(message);
    this.name = 'VexinterError';
  }
}

export class RejectionError extends VexinterError {
  constructor(
    public readonly reason: RejectionReason,
    message: string,
    statusCode: number = 422,
    details?: unknown
  ) {
    super(message, statusCode, 'REJECTION', details);
    this.name = 'RejectionError';
  }
}

export class RemoteError extends VexinterError {
  constructor(statusCode: number, detail: unknown) {
    super(
      typeof detail === 'string' ? detail : JSON.stringify(detail),
      statusCode,
      'REMOTE_ERROR',
      detail
    );
    this.name = 'RemoteError';
  }
}

export interface VexinterConfig {
  endpoint: string;
  adminToken?: string;
  timeout?: number;
}

export interface MetricsSnapshot {
  executions: number;
  false_successes_detected: number;
  discrepancies: number;
  retries_or_compensations: number;
  human_decisions_requested: number;
  human_decisions_made: number;
  rejected_operations: number;
  authentication_errors: number;
  integrity_failures: number;
  final_states: Record<string, number>;
  message_rejections_by_reason: Record<string, number>;
  tool_rejections_by_reason: Record<string, number>;
}

export interface EraseBody {
  execution_ids: string[];
  reason?: string;
}

export interface EraseResult {
  erased: string[];
  rejected: Record<string, string>;
}

export interface CaseDetail {
  case: RecoveryCase;
  options: CaseOption[];
  context: Record<string, unknown>;
}

export interface CasePreview {
  verdict: VerdictResult;
  plan: Plan | null;
  can_retry: boolean;
  can_compensate: boolean;
  can_reverify: boolean;
}

export interface AdminSummary {
  agents: number;
  instances: number;
  credentials: number;
  tasks: number;
  executions: number;
  cases_pending: number;
  cases_resolved: number;
  messages_pending: number;
  storage: {
    streams: number;
    events: number;
  };
}

export interface ChainVerifyResult {
  ok: boolean;
  stream: string;
  events_checked: number;
  head_seq: number;
  head_hash: string;
  problems: Array<{
    seq: number;
    expected_hash: string;
    actual_hash: string;
    problem: string;
  }>;
}

// EraseResult defined in generated spec

export interface Plan {
  steps: PlanStep[];
}

export interface PlanStep {
  tool: string;
  args: Record<string, unknown>;
  condition?: ConditionNode;
  compensation?: {
    tool: string;
    args: Record<string, unknown>;
  };
}