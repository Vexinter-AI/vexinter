/**
 * Vexinter TypeScript SDK - Main Entry Point
 *
 * @packageDocumentation
 * Provides verified, recoverable, and secure infrastructure for AI agents.
 */

// Types
export * from './types';

// Crypto utilities
export * from './crypto/index.js';

// Identity and messaging
export * from './identity/index.js';

// Runtime (async)
export { AsyncHttpClient, invokeTool, makeToolNamespace } from './runtime/client.js';
export type { ToolNamespace, SystemNamespace, ToolFn } from './runtime/client.js';
export { AsyncRemoteTask } from './runtime/task.js';
export { AsyncRemoteRuntime } from './runtime/runtime.js';

// Version
export const VERSION = '0.5.1';

/* Prototype: review the security notes in README.md before using this package. */
