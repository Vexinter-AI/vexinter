import { canonicalJson, sha256Hex, createAgentKeyMaterial, generateSequence } from './dist/crypto/index.js';
import { RemoteCredentials, buildAuthHeaders } from './dist/identity/index.js';

// Test 1: canonicalJson
const obj1 = { z: 1, a: 'test', m: { b: 2, a: 1 } };
const obj2 = { m: { a: 1, b: 2 }, z: 1, a: 'test' };
const json1 = canonicalJson(obj1);
const json2 = canonicalJson(obj2);
console.assert(json1 === json2, 'canonicalJson failed');
console.assert(json1 === '{"a":"test","m":{"a":1,"b":2},"z":1}', 'canonicalJson output mismatch');
console.log('✅ canonicalJson test passed');

// Test 2: sha256Hex
const hash = sha256Hex('hello world');
console.assert(hash === 'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9', 'sha256Hex failed');
console.log('✅ sha256Hex test passed');

// Test 3: createAgentKeyMaterial
const keys = createAgentKeyMaterial();
console.assert(keys.signing_priv instanceof Uint8Array, 'signing_priv not Uint8Array');
console.assert(keys.signing_pub instanceof Uint8Array, 'signing_pub not Uint8Array');
console.assert(keys.encryption_priv instanceof Uint8Array, 'encryption_priv not Uint8Array');
console.assert(keys.encryption_pub instanceof Uint8Array, 'encryption_pub not Uint8Array');
console.log('✅ createAgentKeyMaterial test passed');

// Test 4: generateSequence
const seq = generateSequence();
console.assert(typeof seq === 'bigint', 'generateSequence not bigint');
console.log('✅ generateSequence test passed');

// Test 5: RemoteCredentials
const creds = new RemoteCredentials('agent1', 'instance1', 'cred1', keys);
console.assert(creds.agent_id === 'agent1', 'agent_id mismatch');
console.assert(creds.instance_id === 'instance1', 'instance_id mismatch');
console.assert(creds.credential_id === 'cred1', 'credential_id mismatch');
console.log('✅ RemoteCredentials test passed');

// Test 6: buildAuthHeaders
const headers = buildAuthHeaders('POST', '/v1/test', new TextEncoder().encode('{}'), creds);
console.assert(headers['X-Vex-Credential'] === 'cred1', 'credential header mismatch');
console.assert(headers['Content-Type'] === 'application/json', 'content-type mismatch');
console.log('✅ buildAuthHeaders test passed');

console.log('\n✅ All tests passed!');