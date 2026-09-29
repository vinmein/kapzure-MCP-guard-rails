import { randomUUID, timingSafeEqual } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import Fastify from 'fastify';
import { prepareConfig, tokenDigest } from './config.js';
import { checkDepth, has, isObject, Rejection, strictJson } from './json.js';
import { checkTool, Limiter } from './policy.js';
import { sendUpstream } from './upstream.js';

const VERSIONS = new Set(['2025-03-26', '2025-06-18', '2025-11-25']);
const METHODS = new Set(['initialize', 'notifications/initialized', 'ping', 'tools/list', 'tools/call']);
const validId = value => typeof value === 'string' || Number.isSafeInteger(value);
const mediaType = value => (value ?? '').split(';')[0].trim();

function validateMessage(value, settings) {
  if (!isObject(value) || value.jsonrpc !== '2.0' ||
      Object.keys(value).some(key => !['jsonrpc', 'id', 'method', 'params'].includes(key))) {
    throw new Rejection(400, -32600, 'Expected one JSON-RPC 2.0 message');
  }
  if (has(value, 'id') && !validId(value.id)) throw new Rejection(400, -32600, 'Invalid request ID');
  if (typeof value.method !== 'string') throw new Rejection(400, -32600, 'Method must be a string');
  if (!METHODS.has(value.method)) throw new Rejection(403, -32601, 'Method is not permitted');
  if ((value.method === 'notifications/initialized') === has(value, 'id')) {
    throw new Rejection(400, -32600, 'Invalid request or notification ID');
  }
  const params = has(value, 'params') ? value.params : {};
  if (!isObject(params)) throw new Rejection(400, -32602, 'Params must be an object');
  checkDepth(value, settings.max_json_depth);
  return params;
}

export function createApp(config, { env = process.env, transport = sendUpstream,
  audit = record => process.stdout.write(JSON.stringify(record) + '\n'), clock } = {}) {
  const { settings, validators, credentials, upstreamToken } = prepareConfig(config, env);
  const limiter = new Limiter(clock);
  let inFlight = 0;
  const timeoutMs = Math.ceil(settings.timeout_seconds * 1000);
  const app = Fastify({ logger: false, bodyLimit: settings.max_request_bytes,
    requestTimeout: timeoutMs, connectionTimeout: timeoutMs, exposeHeadRoutes: false });
  app.decorateRequest('guard', null);

  app.removeContentTypeParser('application/json');
  app.addContentTypeParser('application/json', { parseAs: 'buffer' }, (request, body, done) => {
    try { done(null, strictJson(body)); }
    catch { done(new Rejection(400, -32700, 'Invalid JSON')); }
  });

  app.addHook('onRequest', async (request, reply) => {
    if (request.routeOptions.url !== '/mcp') return;
    request.guard = { started: performance.now(), trace_id: randomUUID(), client: null,
      method: null, tool: null, requestId: null, reason: 'Internal error', timer: null };
    reply.header('X-Request-ID', request.guard.trace_id).header('Cache-Control', 'no-store');
    const headers = request.headers;
    if (headers.origin !== undefined && !settings.allowed_origins.includes(headers.origin)) {
      throw new Rejection(403, -32003, 'Origin is not allowed');
    }
    const match = /^Bearer (.+)$/i.exec(headers.authorization ?? '');
    if (match) {
      const digest = tokenDigest(match[1]);
      for (const [name, expected] of credentials) {
        if (timingSafeEqual(digest, expected)) request.guard.client = name;
      }
    }
    if (request.guard.client === null) throw new Rejection(401, -32001, 'Valid bearer authentication is required');
    limiter.consume(request.guard.client, 'all', settings.clients[request.guard.client].rate_limit);
    if (request.method !== 'POST') throw new Rejection(405, -32000, 'Only POST is supported in stateless JSON mode');
    if (has(headers, 'mcp-session-id') || has(headers, 'last-event-id')) {
      throw new Rejection(400, -32600, 'Sessions and stream resumption are unsupported');
    }
    if (!VERSIONS.has(headers['mcp-protocol-version'] ?? '2025-03-26')) {
      throw new Rejection(400, -32600, 'Unsupported MCP protocol version');
    }
    if (mediaType(headers['content-type']) !== 'application/json') {
      throw new Rejection(415, -32600, 'Content-Type must be application/json');
    }
    if ((headers['content-encoding'] ?? 'identity') !== 'identity') {
      throw new Rejection(415, -32600, 'Compressed requests are unsupported');
    }
    // Bound total body-read time, even when bytes trickle in continuously.
    request.guard.timer = setTimeout(() => {
      if (!reply.sent) {
        request.guard.reason = 'Request body timed out';
        reply.header('Connection', 'close').code(408).send({ jsonrpc: '2.0', id: null,
          error: { code: -32000, message: 'Request body timed out' } });
      }
    }, timeoutMs);
    request.guard.timer.unref();
  });
  app.addHook('preValidation', async request => clearTimeout(request.guard?.timer));
  app.addHook('onResponse', async (request, reply) => {
    if (!request.guard) return;
    const { started, timer, requestId, ...record } = request.guard;
    clearTimeout(timer);
    audit({ event: 'mcp_request', ...record, status: reply.statusCode,
      duration_ms: Math.round((performance.now() - started) * 100) / 100 });
  });
  app.setErrorHandler((error, request, reply) => {
    clearTimeout(request.guard?.timer);
    let rejection = error;
    if (!(error instanceof Rejection)) {
      if (error.code === 'FST_ERR_CTP_BODY_TOO_LARGE') rejection = new Rejection(413, -32600, 'Request exceeds size limit');
      else if (error.statusCode >= 400 && error.statusCode < 500) rejection = new Rejection(error.statusCode, -32600, 'Invalid HTTP request');
      else rejection = new Rejection(500, -32603, 'Internal gateway error');
    }
    if (request.guard) request.guard.reason = rejection.message;
    if (rejection.status === 401) reply.header('WWW-Authenticate', 'Bearer');
    if (rejection.status === 405) reply.header('Allow', 'POST');
    if (rejection.retryAfter) reply.header('Retry-After', String(rejection.retryAfter));
    reply.code(rejection.status).send({ jsonrpc: '2.0', id: request.guard?.requestId ?? null,
      error: { code: rejection.code, message: rejection.message } });
  });

  async function forward(message, version) {
    const headers = { 'content-type': 'application/json', accept: 'application/json, text/event-stream',
      'accept-encoding': 'identity', 'mcp-protocol-version': version };
    if (upstreamToken) headers.authorization = `Bearer ${upstreamToken}`;
    let response;
    try {
      response = await transport(settings.upstream_url, { headers, body: JSON.stringify(message),
        maxBytes: settings.max_response_bytes, timeoutMs });
    } catch (error) {
      if (error instanceof Rejection) throw error;
      throw new Rejection(502, -32002, 'Upstream transport failed');
    }
    if (response.status < 200 || response.status >= 300) throw new Rejection(502, -32002, 'Upstream rejected the request');
    if (has(response.headers, 'mcp-session-id')) throw new Rejection(502, -32002, 'Upstream must use stateless HTTP mode');
    if (!has(message, 'id')) {
      if (response.status !== 202) throw new Rejection(502, -32002, 'Invalid upstream notification response');
      return null;
    }
    if (mediaType(response.headers['content-type']) !== 'application/json') {
      throw new Rejection(502, -32002, 'Upstream must use JSON response mode');
    }
    if ((response.headers['content-encoding'] ?? 'identity') !== 'identity') {
      throw new Rejection(502, -32002, 'Compressed upstream responses are unsupported');
    }
    if (response.body.length > settings.max_response_bytes) throw new Rejection(502, -32002, 'Upstream response exceeds size limit');
    try {
      const value = strictJson(response.body);
      checkDepth(value, settings.max_json_depth);
      if (!isObject(value) || value.jsonrpc !== '2.0' || value.id !== message.id ||
          Object.keys(value).length !== 3 || (has(value, 'result') === has(value, 'error'))) throw new Error();
      if (has(value, 'error')) {
        if (!isObject(value.error) || !Number.isSafeInteger(value.error.code) ||
            typeof value.error.message !== 'string') throw new Error();
        value.error = { code: value.error.code, message: 'Upstream MCP error' };
      } else if (!isObject(value.result)) throw new Error();
      return value;
    } catch { throw new Rejection(502, -32002, 'Invalid upstream JSON-RPC response'); }
  }

  app.get('/healthz', async () => ({ status: 'ok' }));
  app.route({ method: ['POST', 'GET', 'DELETE', 'PUT', 'PATCH', 'OPTIONS', 'HEAD'], url: '/mcp',
    handler: async (request, reply) => {
      const message = request.body;
      if (isObject(message) && validId(message.id)) request.guard.requestId = message.id;
      const params = validateMessage(message, settings);
      const method = message.method;
      request.guard.method = method;
      let version = request.headers['mcp-protocol-version'] ?? '2025-03-26';
      if (method === 'tools/call') {
        request.guard.tool = checkTool(request.guard.client, params, settings, validators);
        const rate = settings.tools[request.guard.tool].rate_limit;
        if (rate) limiter.consume(request.guard.client, 'tool:' + request.guard.tool, rate);
      } else if (method === 'initialize') {
        if (!VERSIONS.has(params.protocolVersion) || !isObject(params.capabilities) ||
            !isObject(params.clientInfo) || typeof params.clientInfo.name !== 'string' ||
            typeof params.clientInfo.version !== 'string') {
          throw new Rejection(400, -32602, 'Invalid or unsupported initialization parameters');
        }
        version = params.protocolVersion;
        message.params = { protocolVersion: version, capabilities: {}, clientInfo: { name: 'mcp-guard-js', version: '0.1.0' } };
      } else if (method === 'tools/list') {
        if (Object.keys(params).some(key => key !== 'cursor') || (has(params, 'cursor') && typeof params.cursor !== 'string')) {
          throw new Rejection(400, -32602, 'Unsupported tools/list parameters');
        }
      } else if (Object.keys(params).length) throw new Rejection(400, -32602, 'This method takes no parameters');
      if (inFlight >= settings.max_in_flight) throw new Rejection(503, -32000, 'Gateway is at capacity', 1);
      inFlight++;
      let value;
      try { value = await forward(message, version); }
      finally { inFlight--; }
      if (value && has(value, 'result')) {
        const result = value.result;
        if (method === 'tools/list') {
          if (!Array.isArray(result.tools) || result.tools.some(tool => !isObject(tool) || typeof tool.name !== 'string')) {
            throw new Rejection(502, -32002, 'Invalid upstream tool catalog');
          }
          const allowed = settings.clients[request.guard.client].allowed_tools;
          value.result = { tools: result.tools.filter(tool => allowed.includes(tool.name))
            .map(tool => ({ ...tool, inputSchema: settings.tools[tool.name].input_schema })) };
          if (has(result, 'nextCursor')) {
            if (typeof result.nextCursor !== 'string') throw new Rejection(502, -32002, 'Invalid upstream pagination cursor');
            value.result.nextCursor = result.nextCursor;
          }
        } else if (method === 'initialize') {
          if (!VERSIONS.has(result.protocolVersion)) throw new Rejection(502, -32002, 'Unsupported upstream protocol version');
          value.result = { protocolVersion: result.protocolVersion, capabilities: { tools: {} },
            serverInfo: { name: 'mcp-guard-js', version: '0.1.0' } };
        }
      }
      request.guard.reason = value && has(value, 'error') ? 'Upstream MCP error' : 'Allowed';
      if (value === null) return reply.code(202).send();
      return value;
    },
  });
  return app;
}
