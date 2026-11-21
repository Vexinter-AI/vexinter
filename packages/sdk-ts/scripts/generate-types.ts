#!/usr/bin/env node
/**
 * Phase 6: Generate TypeScript types from OpenAPI spec
 * This script fetches the OpenAPI spec from the service and generates
 * TypeScript types that match the API contracts exactly.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OPENAPI_PATH = join(__dirname, '..', 'openapi.json');
const OUTPUT_DIR = join(__dirname, '..', 'src', 'types', 'generated');

interface OpenAPISpec {
  components: {
    schemas: Record<string, unknown>;
  };
  paths: Record<string, Record<string, unknown>>;
}

function toPascalCase(str: string): string {
  return str
    .replace(/[-_\s]+(.)?/g, (_, c) => c ? c.toUpperCase() : '')
    .replace(/^(.)/, c => c.toUpperCase());
}

function sanitizeName(name: string): string {
  return name
    .replace(/[^a-zA-Z0-9_]/g, '_')
    .replace(/^_+|_+$/g, '');
}

function generateType(schema: unknown, seen = new Set<string>()): string {
  if (!schema || typeof schema !== 'object') {
    return 'unknown';
  }

  const s = schema as Record<string, unknown>;

  // Handle $ref
  if (s.$ref) {
    const refName = s.$ref.split('/').pop() || 'Unknown';
    return toPascalCase(refName);
  }

  // Handle anyOf / oneOf
  if (s.anyOf || s.oneOf) {
    const types = (s.anyOf || s.oneOf) as unknown[];
    return types.map(t => generateType(t, seen)).join(' | ');
  }

  // Handle allOf
  if (s.allOf) {
    const types = s.allOf as unknown[];
    return types.map(t => generateType(t, seen)).join(' & ');
  }

  // Handle enum
  if (s.enum) {
    return (s.enum as unknown[]).map(v => JSON.stringify(v)).join(' | ');
  }

  // Handle type
  const type = s.type as string | undefined;
  if (type) {
    switch (type) {
      case 'string':
        return 'string';
      case 'integer':
      case 'number':
        return 'number';
      case 'boolean':
        return 'boolean';
      case 'array':
        if (s.items) {
          return `${generateType(s.items, seen)}[]`;
        }
        return 'unknown[]';
      case 'object':
        if (s.properties) {
          const props = s.properties as Record<string, unknown>;
          const required = (s.required as string[]) || [];
          const entries = Object.entries(props).map(([key, value]) => {
            const optional = !required.includes(key) ? '?' : '';
            return `  ${key}${optional}: ${generateType(value, seen)};`;
          });
          return `{\n${entries.join('\n')}\n}`;
        }
        if (s.additionalProperties) {
          const valType = generateType(s.additionalProperties, seen);
          return `Record<string, ${valType}>`;
        }
        return 'Record<string, unknown>';
      default:
        return 'unknown';
    }
  }

  return 'unknown';
}

function generateInterface(name: string, schema: unknown): string {
  const typeDef = generateType(schema);
  if (typeDef.startsWith('{')) {
    return `export interface ${name} ${typeDef}`;
  } else {
    // It's a reference or primitive - use type alias
    return `export type ${name} = ${typeDef};`;
  }
}

function main() {
  if (!existsSync(OPENAPI_PATH)) {
    console.error('OpenAPI spec not found at:', OPENAPI_PATH);
    process.exit(1);
  }

  const spec = JSON.parse(readFileSync(OPENAPI_PATH, 'utf-8')) as OpenAPISpec;
  mkdirSync(OUTPUT_DIR, { recursive: true });

  const schemas = spec.components?.schemas || {};
  const typeNames = new Set<string>();

  // Types to skip - defined manually in SDK
  const skipTypes = new Set([
    'EraseBody',
    'EraseResult',
    'CaseDetail',
    'CasePreview',
    'AdminSummary',
    'ChainVerifyResult',
    'Plan',
    'PlanStep',
    'ValidationError',
    'HTTPValidationError',
  ]);

  // Response type mappings for operation IDs to actual types from main index
  const responseTypeMap: Record<string, string> = {
    'create_task_v1_tasks_post': 'TaskCreatedResponse',
    'list_tasks_v1_tasks_get': 'TaskSummary[]',
    'invoke_tool_v1_tasks_task_id_invoke_post': 'ToolResult',
    'verify_task_v1_tasks_task_id_verify_post': 'VerdictResult',
    'recover_task_v1_tasks_task_id_recover_post': 'RecoveryCase',
    'run_plan_v1_tasks_task_id_plan_post': 'VerdictResult',
    'complete_task_v1_tasks_task_id_complete_post': 'void',
    'add_collaborator_v1_tasks_task_id_collaborators_post': 'void',
    'get_task_v1_tasks_task_id_get': 'TaskDetail',
    'task_events_v1_tasks_task_id_events_get': 'Event[]',
    'task_graph_v1_tasks_task_id_graph_get': 'ExecutionGraph',
    'task_audit_v1_tasks_task_id_audit_get': 'ChainAudit',
    'append_event_v1_streams_stream_events_post': 'Event',
    'privacy_erase_v1_privacy_erase_post': 'EraseResult',
    'query_events_v1_events_get': 'Event[]',
    'send_message_v1_messages_post': 'MessageSent',
    'inbox_v1_messages_inbox_get': 'Envelope[]',
    'ack_message_v1_messages_ack_post': 'void',
    'msg_sequence_v1_messages_sequence_get': 'number',
    'enc_key_v1_identity_key_get': 'string',
    'credential_public_v1_identity_credentials_cid_public_get': 'Credential',
    'rotate_own_credential_v1_identity_credentials_rotate_post': 'Credential',
    'memory_v1_memory_get': 'MemoryEpisode[]',
    'enroll_v1_identity_agents_post': 'Agent',
    'list_agents_v1_identity_agents_get': 'Agent[]',
    'spawn_v1_identity_instances_post': 'Instance',
    'list_instances_v1_identity_instances_get': 'Instance[]',
    'issue_v1_identity_credentials_post': 'Credential',
    'list_credentials_v1_identity_credentials_get': 'Credential[]',
    'revoke_cred_v1_identity_credentials_cid_delete': 'void',
    'revoke_inst_v1_identity_instances_iid_revoke_post': 'void',
    'cases_v1_cases_get': 'RecoveryCase[]',
    'decide_v1_cases_case_id_decide_post': 'VerdictResult',
    'run_due_v1_followups_run_due_post': '{ executed: number }',
    'checkpoint_v1_checkpoints_post': '{ ok: boolean; seq: number }',
    'metrics_v1_metrics_get': 'MetricsSnapshot',
    'prometheus_metrics_metrics_get': 'string',
    'chains_v1_chains_verify_get': 'ChainVerifyResult',
    'case_detail_json_v1_cases_case_id_get': 'CaseDetail',
    'case_preview_json_v1_cases_case_id_preview_post': 'CasePreview',
    'admin_summary_v1_admin_summary_get': 'AdminSummary',
    'admin_task_v1_admin_tasks_task_id_get': 'TaskDetail',
  };

  // Generate types for each schema
  const generatedTypes: string[] = [
    '// Auto-generated from OpenAPI spec - DO NOT EDIT MANUALLY',
    '// Run: npm run generate:types',
    '',
    "import type { RecoveryCase, VerdictResult, MetricsSnapshot, ChainVerifyResult, CaseDetail, CasePreview, AdminSummary, Plan, TaskCreatedResponse, TaskSummary, TaskDetail, ToolResult, MessageSent, Envelope, Credential, MemoryEpisode, Agent, Instance, EraseResult } from '../index';",
    '',
  ];

  for (const [schemaName, schema] of Object.entries(schemas)) {
    const typeName = toPascalCase(schemaName);
    if (typeNames.has(typeName) || skipTypes.has(typeName)) continue;
    typeNames.add(typeName);

    const typeDef = generateInterface(typeName, schema);
    generatedTypes.push(typeDef);
    generatedTypes.push('');
  }

  // Generate API request/response types from paths
  generatedTypes.push('// API Request/Response types');
  generatedTypes.push('');

  for (const [path, methods] of Object.entries(spec.paths || {})) {
    // Skip console endpoints - not part of the main SDK
    if (path.startsWith('/console')) continue;

    for (const [method, details] of Object.entries(methods)) {
      const opId = (details as Record<string, unknown>).operationId as string;
      if (!opId) continue;

      const baseName = toPascalCase(opId);

      // Request body
      const requestBody = (details as Record<string, unknown>).requestBody as Record<string, unknown> | undefined;
      if (requestBody?.content) {
        const content = requestBody.content as Record<string, unknown>;
        const jsonContent = content['application/json'] as Record<string, unknown>;
        if (jsonContent?.schema) {
          const reqName = `${baseName}Request`;
          if (!typeNames.has(reqName) && !skipTypes.has(reqName)) {
            typeNames.add(reqName);
            const schema = jsonContent.schema;
            // Check if it's a $ref
            if (typeof schema === 'object' && schema && '$ref' in schema) {
              const refName = (schema as Record<string, string>).$ref.split('/').pop() || 'Unknown';
              if (!skipTypes.has(toPascalCase(refName))) {
                generatedTypes.push(`export type ${reqName} = ${toPascalCase(refName)};`);
              }
            } else {
              const typeDef = generateInterface(reqName, schema);
              generatedTypes.push(typeDef);
            }
            generatedTypes.push('');
          }
        }
      }

      // Responses - prefer 200 status code, use our mapping for known operation IDs
      const responses = (details as Record<string, unknown>).responses as Record<string, unknown>;
      const successResponse = responses['200'] as Record<string, unknown> | undefined;
      if (successResponse?.content) {
        const content = successResponse.content as Record<string, unknown>;
        const jsonContent = content['application/json'] as Record<string, unknown>;
        if (jsonContent?.schema) {
          const respName = `${baseName}Response`;
          if (!typeNames.has(respName) && !skipTypes.has(respName)) {
            typeNames.add(respName);
            const schema = jsonContent.schema;
            
            // Check if we have a manual mapping for this operation
            if (responseTypeMap[opId]) {
              generatedTypes.push(`export type ${respName} = ${responseTypeMap[opId]};`);
              generatedTypes.push('');
            } else if (typeof schema === 'object' && schema && '$ref' in schema) {
              const refName = (schema as Record<string, string>).$ref.split('/').pop() || 'Unknown';
              if (!skipTypes.has(toPascalCase(refName))) {
                generatedTypes.push(`export type ${respName} = ${toPascalCase(refName)};`);
                generatedTypes.push('');
              }
            } else {
              const typeDef = generateInterface(respName, schema);
              generatedTypes.push(typeDef);
              generatedTypes.push('');
            }
          }
        }
      }
    }
  }

  const output = generatedTypes.join('\n');
  const outputPath = join(OUTPUT_DIR, 'api.ts');
  writeFileSync(outputPath, output);
  console.log(`Generated ${typeNames.size} types to ${outputPath}`);

  // Also generate a barrel export
  const barrelPath = join(OUTPUT_DIR, 'index.ts');
  writeFileSync(barrelPath, `export * from './api';\n`);
  console.log(`Generated barrel export to ${barrelPath}`);
}

main();