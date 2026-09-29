import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { isObject, strictJson } from './json.js';

const integer = (minimum, maximum, value) => ({ type: 'integer', minimum, maximum, default: value });
const strings = { type: 'array', items: { type: 'string' } };
const rate = {
  type: 'object', additionalProperties: false, default: {},
  properties: { requests: integer(1, 100000, 60), window_seconds: integer(1, 86400, 60) },
};
const schema = {
  type: 'object', additionalProperties: false, required: ['upstream_url', 'clients', 'tools'],
  properties: {
    upstream_url: { type: 'string', minLength: 1 },
    upstream_token_env: { type: ['string', 'null'], default: null },
    allowed_origins: { ...strings, default: [] },
    max_request_bytes: integer(1024, 10485760, 65536),
    max_response_bytes: integer(1024, 104857600, 1048576),
    timeout_seconds: { type: 'number', exclusiveMinimum: 0, maximum: 300, default: 30 },
    max_in_flight: integer(1, 1000, 32),
    max_json_depth: integer(1, 64, 32),
    clients: {
      type: 'object', minProperties: 1, additionalProperties: {
        type: 'object', additionalProperties: false, required: ['token_env', 'allowed_tools'],
        properties: { token_env: { type: 'string', minLength: 1 }, allowed_tools: strings, rate_limit: rate },
      },
    },
    tools: {
      type: 'object', additionalProperties: {
        type: 'object', additionalProperties: false, required: ['input_schema'],
        properties: {
          input_schema: { type: 'object' },
          blocked_substrings: { type: 'array', items: { type: 'string', minLength: 1 }, default: [] },
          rate_limit: { anyOf: [{ ...rate, default: undefined }, { type: 'null' }], default: null },
        },
      },
    },
  },
};
const validateConfig = new Ajv2020({ useDefaults: true, strict: false, ownProperties: true }).compile(schema);

export function secret(name, env) {
  const value = env[name];
  if (typeof value !== 'string' || !value || /\s/u.test(value)) {
    throw new Error(`Set ${name} to a nonempty token without whitespace`);
  }
  return value;
}

export const tokenDigest = value => createHash('sha256').update(value).digest();

export function prepareConfig(input, env = process.env) {
  const settings = structuredClone(input);
  if (!validateConfig(settings)) throw new Error('Invalid policy configuration');
  const url = new URL(settings.upstream_url);
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname ||
      url.username || url.password || url.hash || url.search) {
    throw new Error('upstream_url must be HTTP(S), without credentials, query, or fragment');
  }
  const validators = new Map();
  for (const [name, tool] of Object.entries(settings.tools)) {
    if (tool.input_schema.type !== 'object') throw new Error('Tool input_schema must have type object');
    const pending = [tool.input_schema];
    while (pending.length) {
      const value = pending.pop();
      if (isObject(value)) {
        for (const [key, child] of Object.entries(value)) {
          if (key === '$ref' || key === '$dynamicRef') throw new Error('Inline schemas; references are unsupported');
          pending.push(child);
        }
      } else if (Array.isArray(value)) pending.push(...value);
    }
    // Each tool owns its schema namespace; compilation never fetches remote refs.
    const ajv = new Ajv2020({ strict: true, allErrors: false, ownProperties: true });
    addFormats(ajv);
    validators.set(name, ajv.compile(tool.input_schema));
    if (tool.rate_limit) {
      tool.rate_limit.requests ??= 60;
      tool.rate_limit.window_seconds ??= 60;
    }
  }
  const credentials = new Map();
  const usedTokens = new Set();
  for (const [name, client] of Object.entries(settings.clients)) {
    if (client.allowed_tools.some(tool => !validators.has(tool))) throw new Error('Undefined tool in client allowlist');
    const token = secret(client.token_env, env);
    if ([...token].length < 32) throw new Error(`${client.token_env} must contain at least 32 characters`);
    const digest = tokenDigest(token);
    if (usedTokens.has(digest.toString('hex'))) throw new Error('Client tokens must be distinct');
    usedTokens.add(digest.toString('hex'));
    credentials.set(name, digest);
  }
  const upstreamToken = settings.upstream_token_env ? secret(settings.upstream_token_env, env) : null;
  return { settings, validators, credentials, upstreamToken };
}

export function loadConfig(path) {
  return strictJson(readFileSync(path));
}
