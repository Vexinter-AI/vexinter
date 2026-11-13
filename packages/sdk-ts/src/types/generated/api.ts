// Auto-generated from OpenAPI spec - DO NOT EDIT MANUALLY
// Run: npm run generate:types

import type { RecoveryCase, VerdictResult, MetricsSnapshot, ChainVerifyResult, CaseDetail, CasePreview, AdminSummary, Plan, TaskCreatedResponse, TaskSummary, TaskDetail, ToolResult, MessageSent, Envelope, Credential, MemoryEpisode, Agent, Instance, EraseResult } from '../index';

export interface AckBody {
  message_id: string;
  body_hash: string;
}

export interface CollaboratorBody {
  agent: string;
}

export interface CompleteBody {
  exception?: boolean;
  last_status?: string | unknown;
}

export interface CredIssueBody {
  instance_id: string;
  signing_pub: string;
  encryption_pub: string;
}

export interface DecideBody {
  action: string;
  operator?: string;
}

export interface EnrollBody {
  agent_id: string;
  display_name?: string;
  allowed_tools?: string[] | unknown;
  allowed_recipients?: string[] | unknown;
}

export interface EnvelopeBody {
  envelope: Record<string, unknown>;
}

export interface EventBody {
  type: string;
  payload?: Record<string, unknown> | unknown;
  task_id?: string | unknown;
  execution_id?: string | unknown;
  agent_id?: string | unknown;
  operation_id?: string | unknown;
  caused_by?: string | unknown;
  depends_on?: string[];
}

export interface InstanceBody {
  agent_id: string;
}

export interface InvokeBody {
  tool: string;
  args?: Record<string, unknown>;
  agent?: string | unknown;
  depends_on?: string[];
  idempotency_key?: string | unknown;
}

export interface PlanBody {
  plan: Record<string, unknown>;
}

export interface RecoverBody {
  failed_tool?: string | unknown;
  invoked_event?: string | unknown;
}

export interface RevokeBody {
  reason?: string;
}

export interface RotateBody {
  signing_pub: string;
  encryption_pub: string;
}

export interface TaskBody {
  agent?: string | unknown;
  objective: string;
  condition: Record<string, unknown>;
  input?: Record<string, unknown>;
  snapshot?: Record<string, unknown>[];
}

// API Request/Response types

export type CreateTaskV1TasksPostRequest = TaskBody;

export type CreateTaskV1TasksPostResponse = TaskCreatedResponse;

export type ListTasksV1TasksGetResponse = TaskSummary[];

export type InvokeToolV1TasksTaskIdInvokePostRequest = InvokeBody;

export type InvokeToolV1TasksTaskIdInvokePostResponse = unknown;

export type VerifyTaskV1TasksTaskIdVerifyPostResponse = unknown;

export type RecoverTaskV1TasksTaskIdRecoverPostRequest = RecoverBody;

export type RecoverTaskV1TasksTaskIdRecoverPostResponse = unknown;

export type RunPlanV1TasksTaskIdPlanPostRequest = PlanBody;

export type RunPlanV1TasksTaskIdPlanPostResponse = unknown;

export type CompleteTaskV1TasksTaskIdCompletePostRequest = CompleteBody;

export type CompleteTaskV1TasksTaskIdCompletePostResponse = unknown;

export type AddCollaboratorV1TasksTaskIdCollaboratorsPostRequest = CollaboratorBody;

export type AddCollaboratorV1TasksTaskIdCollaboratorsPostResponse = unknown;

export type GetTaskV1TasksTaskIdGetResponse = unknown;

export type TaskEventsV1TasksTaskIdEventsGetResponse = unknown;

export type TaskGraphV1TasksTaskIdGraphGetResponse = unknown;

export type TaskAuditV1TasksTaskIdAuditGetResponse = unknown;

export type AppendEventV1StreamsStreamEventsPostRequest = EventBody;

export type AppendEventV1StreamsStreamEventsPostResponse = unknown;


export type PrivacyEraseV1PrivacyErasePostResponse = EraseResult;

export type QueryEventsV1EventsGetResponse = Event[];

export type SendMessageV1MessagesPostRequest = EnvelopeBody;

export type SendMessageV1MessagesPostResponse = MessageSent;

export type InboxV1MessagesInboxGetResponse = Envelope[];

export type AckMessageV1MessagesAckPostRequest = AckBody;

export type AckMessageV1MessagesAckPostResponse = void;

export type MsgSequenceV1MessagesSequenceGetResponse = number;

export type EncKeyV1IdentityKeyGetResponse = string;

export type CredentialPublicV1IdentityCredentialsCidPublicGetResponse = unknown;

export type RotateOwnCredentialV1IdentityCredentialsRotatePostRequest = RotateBody;

export type RotateOwnCredentialV1IdentityCredentialsRotatePostResponse = Credential;

export type MemoryV1MemoryGetResponse = MemoryEpisode[];

export type EnrollV1IdentityAgentsPostRequest = EnrollBody;

export type EnrollV1IdentityAgentsPostResponse = Agent;

export type ListAgentsV1IdentityAgentsGetResponse = Agent[];

export type SpawnV1IdentityInstancesPostRequest = InstanceBody;

export type SpawnV1IdentityInstancesPostResponse = Instance;

export type ListInstancesV1IdentityInstancesGetResponse = Instance[];

export type IssueV1IdentityCredentialsPostRequest = CredIssueBody;

export type IssueV1IdentityCredentialsPostResponse = Credential;

export type ListCredentialsV1IdentityCredentialsGetResponse = Credential[];

export type RevokeCredV1IdentityCredentialsCidDeleteResponse = unknown;

export type RevokeInstV1IdentityInstancesIidRevokePostRequest = RevokeBody;

export type RevokeInstV1IdentityInstancesIidRevokePostResponse = unknown;

export type CasesV1CasesGetResponse = RecoveryCase[];

export type DecideV1CasesCaseIdDecidePostRequest = DecideBody;

export type DecideV1CasesCaseIdDecidePostResponse = unknown;

export type RunDueV1FollowupsRunDuePostResponse = { executed: number };

export type CheckpointV1CheckpointsPostResponse = { ok: boolean; seq: number };

export type MetricsV1MetricsGetResponse = MetricsSnapshot;

export type PrometheusMetricsMetricsGetResponse = string;

export type ChainsV1ChainsVerifyGetResponse = ChainVerifyResult;

export type CaseDetailJsonV1CasesCaseIdGetResponse = unknown;

export type CasePreviewJsonV1CasesCaseIdPreviewPostResponse = unknown;

export type AdminSummaryV1AdminSummaryGetResponse = AdminSummary;

export type AdminTaskV1AdminTasksTaskIdGetResponse = unknown;
