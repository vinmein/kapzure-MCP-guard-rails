import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createApp } from '../src/app.js';
import { loadConfig, prepareConfig } from '../src/config.js';
import { Limiter } from '../src/policy.js';
import { createDemo } from '../examples/demo-server.js';

const TOKEN = 'a'.repeat(40);
const OTHER = 'b'.repeat(40);
const env = { MCP_GUARD_DEMO_TOKEN: TOKEN, OTHER_TOKEN: OTHER };
const headers = { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json',
  'mcp-protocol-version': '2025-11-25', accept: 'application/json, text/event-stream' };
const config = () => loadConfig(new URL('../policy.json', import.meta.url));
const message = (method = 'tools/call', params = { name: 'echo', arguments: { text: 'hello' } }) =>
  ({ jsonrpc: '2.0', id: 7, method, params });
const rpcResponse = (result = {}, options = {}) => ({ status: 200, headers: { 'content-type': 'application/json' },
  body: Buffer.from(JSON.stringify({ jsonrpc: '2.0', id: 7, result })), ...options });

async function fixture(t, { policy = config(), transport, audit = () => {}, clock } = {}) {
  const calls = [];
  const demo = createDemo();
  const app = createApp(policy, { env, audit, clock, transport: async (url, options) => {
    calls.push({ url, ...options });
    if (transport) return transport(url, options);
    const response = await demo.inject({ method: 'POST', url: '/mcp', headers: options.headers, payload: options.body });
    return { status: response.statusCode, headers: response.headers, body: response.rawPayload };
  } });
  t.after(async () => { await app.close(); await demo.close(); });
  await app.ready();
  const post = (payload = message(), extraHeaders = {}) =>
    app.inject({ method: 'POST', url: '/mcp', headers: { ...headers, ...extraHeaders }, payload });
  return { app, post, calls };
}

test('initialization, notification, discovery, and call against demo server', async t => {
  const { app, post, calls } = await fixture(t);
  const init = await post(message('initialize', { protocolVersion: '2025-11-25', capabilities: { sampling: {} },
    clientInfo: { name: 'test', version: '1' } }));
  assert.equal(init.statusCode, 200);
  assert.deepEqual(init.json().result.capabilities, { tools: {} });
  assert.deepEqual(JSON.parse(calls[0].body).params.capabilities, {});
  const ready = await post({ jsonrpc: '2.0', method: 'notifications/initialized' });
  assert.equal(ready.statusCode, 202);
  assert.equal(ready.body, '');
  const catalog = (await post(message('tools/list', {}))).json().result;
  assert.deepEqual(catalog.tools.map(tool => tool.name), ['echo']);
  assert.equal(catalog.tools[0].inputSchema.additionalProperties, false);
  const result = await post();
  assert.equal(result.statusCode, 200);
  assert.equal(result.json().result.content[0].text, 'hello');
  assert.ok(result.headers['x-request-id']);
  assert.equal(result.headers['cache-control'], 'no-store');
  assert.deepEqual((await app.inject('/healthz')).json(), { status: 'ok' });
});

for (const [name, payload, status] of [
  ['forbidden tool', message('tools/call', { name: 'admin_reset', arguments: {} }), 403],
  ['wrong type', message('tools/call', { name: 'echo', arguments: { text: 9 } }), 400],
  ['extra argument', message('tools/call', { name: 'echo', arguments: { text: 'hi', extra: 1 } }), 400],
  ['missing argument', message('tools/call', { name: 'echo' }), 400],
  ['null argument', message('tools/call', { name: 'echo', arguments: null }), 400],
  ['blocked text', message('tools/call', { name: 'echo', arguments: { text: 'demo_BLOCKED_text' } }), 403],
  ['task parameter', message('tools/call', { name: 'echo', arguments: { text: 'hi' }, task: {} }), 400],
  ['resource method', message('resources/read', { uri: 'file:///secret' }), 403],
  ['catalog metadata', message('tools/list', { _meta: {} }), 400],
  ['bad initialization', message('initialize', { protocolVersion: [] }), 400],
  ['batch', [message(), message()], 400],
  ['tool notification', { jsonrpc: '2.0', method: 'tools/call', params: { name: 'echo' } }, 400],
  ['boolean id', { jsonrpc: '2.0', id: true, method: 'ping' }, 400],
  ['null params', message('ping', null), 400],
  ['prototype tool name', message('tools/call', { name: '__proto__', arguments: {} }), 403],
]) {
  test(`rejects ${name} without forwarding`, async t => {
    const { post, calls } = await fixture(t);
    const response = await post(payload);
    assert.equal(response.statusCode, status);
    assert.equal(calls.length, 0);
  });
}

test('authentication and Origin checks run before parsing the body', async t => {
  const { post, app, calls } = await fixture(t);
  assert.equal((await post('not-json', { authorization: '' })).statusCode, 401);
  assert.equal((await post('not-json', { origin: 'https://evil.example' })).statusCode, 403);
  assert.equal((await app.inject('/mcp')).statusCode, 401);
  for (const method of ['GET', 'DELETE', 'PUT', 'OPTIONS']) {
    const response = await app.inject({ method, url: '/mcp', headers });
    assert.equal(response.statusCode, 405);
    assert.equal(response.headers.allow, 'POST');
  }
  assert.equal(calls.length, 0);
});

for (const [extra, status] of [
  [{ 'mcp-session-id': 'some-session' }, 400], [{ 'last-event-id': 'event' }, 400],
  [{ 'mcp-protocol-version': 'unsupported' }, 400], [{ 'content-encoding': 'gzip' }, 415],
  [{ 'content-type': 'text/plain' }, 415],
]) {
  test(`rejects unsupported headers ${JSON.stringify(extra)}`, async t => {
    const { post, calls } = await fixture(t);
    assert.equal((await post(message(), extra)).statusCode, status);
    assert.equal(calls.length, 0);
  });
}

for (const raw of [
  '{"jsonrpc":"2.0","id":7,"method":"ping","method":"tools/call"}',
  String.raw`{"jsonrpc":"2.0","id":7,"method":"ping","\u006dethod":"tools/call"}`,
  '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
  '{"jsonrpc":"2.0","id":1e999,"method":"ping"}',
  '{"jsonrpc":"2.0","id":9007199254740993,"method":"ping"}',
  String.raw`{"jsonrpc":"2.0","id":"\ud800","method":"ping"}`,
  '{bad', '[[[[',
]) {
  test(`rejects malformed or ambiguous JSON: ${raw}`, async t => {
    const { post, calls } = await fixture(t);
    const response = await post(raw);
    assert.equal(response.statusCode, 400);
    assert.equal(response.json().error.code, -32700);
    assert.equal(calls.length, 0);
  });
}

test('preserves allowed JSON strings and prevents prototype pollution', async t => {
  const { post, calls } = await fixture(t);
  const text = 'braces {} [ ] quotes " \\ emoji 😀';
  assert.equal((await post(message('tools/call', { name: 'echo', arguments: { text } }))).json().result.content[0].text, text);
  const polluted = '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"echo","arguments":{"text":"ok","__proto__":{"polluted":true}}}}';
  assert.equal((await post(polluted)).statusCode, 400);
  assert.equal({}.polluted, undefined);
  assert.equal(calls.length, 1);
});

test('bounds request size and JSON nesting', async t => {
  const policy = config();
  policy.max_request_bytes = 1024;
  policy.max_json_depth = 4;
  const { post, calls } = await fixture(t, { policy });
  assert.equal((await post(message('tools/call', { name: 'echo', arguments: { text: 'x'.repeat(2000) } }))).statusCode, 413);
  assert.equal((await post(message('tools/call', { name: 'echo', arguments: { a: { b: { c: 1 } } } }))).statusCode, 400);
  assert.equal(calls.length, 0);
});

test('client limits and authorization are isolated', async t => {
  const policy = config();
  policy.clients['demo-client'].rate_limit.requests = 1;
  policy.clients.other = { token_env: 'OTHER_TOKEN', allowed_tools: [] };
  const { post } = await fixture(t, { policy });
  assert.equal((await post()).statusCode, 200);
  const limited = await post();
  assert.equal(limited.statusCode, 429);
  assert.ok(Number(limited.headers['retry-after']) >= 1);
  const other = { authorization: `Bearer ${OTHER}` };
  assert.deepEqual((await post(message('tools/list', {}), other)).json().result.tools, []);
  assert.equal((await post(message(), other)).statusCode, 403);
});

test('tool limit leaves ping available', async t => {
  const policy = config();
  policy.tools.echo.rate_limit.requests = 1;
  const { post } = await fixture(t, { policy });
  assert.equal((await post()).statusCode, 200);
  assert.equal((await post()).statusCode, 429);
  assert.equal((await post(message('ping', {}))).statusCode, 200);
});

test('sliding window expires at the boundary', () => {
  let now = 0;
  const limiter = new Limiter(() => now);
  const rate = { requests: 1, window_seconds: 10 };
  limiter.consume('a', 'all', rate);
  now = 9.2;
  assert.throws(() => limiter.consume('a', 'all', rate), error => error.retryAfter === 1);
  now = 10;
  limiter.consume('a', 'all', rate);
});

test('upstream headers contain only gateway-owned values', async t => {
  const policy = config();
  policy.upstream_token_env = 'OTHER_TOKEN';
  const { post, calls } = await fixture(t, { policy });
  await post(message(), { cookie: 'secret=yes', 'x-forwarded-for': 'admin' });
  assert.equal(calls[0].headers.authorization, `Bearer ${OTHER}`);
  assert.equal(calls[0].headers.cookie, undefined);
  assert.equal(calls[0].headers['x-forwarded-for'], undefined);
  assert.equal(calls[0].headers['accept-encoding'], 'identity');
});

for (const [name, response] of [
  ['redirect', rpcResponse({}, { status: 302, headers: { location: 'http://attacker.example' } })],
  ['SSE', rpcResponse({}, { headers: { 'content-type': 'text/event-stream' } })],
  ['session', rpcResponse({}, { headers: { 'content-type': 'application/json', 'mcp-session-id': 'session' } })],
  ['compression', rpcResponse({}, { headers: { 'content-type': 'application/json', 'content-encoding': 'gzip' } })],
  ['wrong ID', rpcResponse({}, { body: Buffer.from('{"jsonrpc":"2.0","id":8,"result":{}}') })],
  ['both result and error', rpcResponse({}, { body: Buffer.from('{"jsonrpc":"2.0","id":7,"result":{},"error":{}}') })],
  ['invalid JSON', rpcResponse({}, { body: Buffer.from('not-json') })],
  ['oversized response', rpcResponse({ text: 'x'.repeat(2000) })],
]) {
  test(`fails closed for upstream ${name}`, async t => {
    const policy = config();
    policy.max_response_bytes = 1024;
    const { post } = await fixture(t, { policy, transport: async () => response });
    assert.equal((await post()).statusCode, 502);
  });
}

test('upstream transport failure is not retried', async t => {
  const { post, calls } = await fixture(t, { transport: async () => { throw new Error('internal secret'); } });
  const response = await post();
  assert.equal(response.statusCode, 502);
  assert.equal(calls.length, 1);
  assert.equal(response.body.includes('internal secret'), false);
});

test('capacity is bounded and released after completion', async t => {
  const policy = config();
  policy.max_in_flight = 1;
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const blocked = new Promise(resolve => { release = resolve; });
  const { post } = await fixture(t, { policy, transport: async () => {
    entered();
    await blocked;
    return rpcResponse();
  } });
  const first = Promise.resolve(post());
  await waiting;
  assert.equal((await post()).statusCode, 503);
  release();
  assert.equal((await first).statusCode, 200);
  assert.equal((await post()).statusCode, 200);
});

test('catalog pagination is preserved for filtered empty pages', async t => {
  const { post } = await fixture(t, { transport: async () => rpcResponse({ tools: [{ name: 'admin_reset' }], nextCursor: 'next' }) });
  assert.deepEqual((await post(message('tools/list', {}))).json().result, { tools: [], nextCursor: 'next' });
});

test('upstream errors are sanitized', async t => {
  const { post } = await fixture(t, { transport: async () => rpcResponse({}, {
    body: Buffer.from(JSON.stringify({ jsonrpc: '2.0', id: 7, error: { code: -32602, message: 'secret', data: 'secret' } })),
  }) });
  const response = await post();
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.json().error, { code: -32602, message: 'Upstream MCP error' });
});

test('audit omits arguments, credentials, and client-supplied IDs', async t => {
  const records = [];
  const { post } = await fixture(t, { audit: record => records.push(record) });
  await post({ ...message('tools/call', { name: 'echo', arguments: { text: 'sensitive-argument' } }), id: 'secret-id' });
  await post(message('tools/call', { name: 'admin_reset', arguments: {} }));
  assert.equal(records.length, 2);
  assert.equal(records[0].client, 'demo-client');
  assert.equal(records[1].status, 403);
  for (const secret of [TOKEN, 'sensitive-argument', 'secret-id']) assert.equal(JSON.stringify(records).includes(secret), false);
});

for (const [name, mutate] of [
  ['unknown tool', value => { value.clients['demo-client'].allowed_tools = ['missing']; }],
  ['unknown setting', value => { value.misspelled = true; }],
  ['external reference', value => { value.tools.echo.input_schema.$ref = 'https://attacker.example/schema'; }],
  ['invalid schema', value => { value.tools.echo.input_schema.properties.text.type = 'wrong'; }],
  ['unsupported format', value => { value.tools.echo.input_schema.properties.text.format = 'unknown'; }],
  ['duplicate token', value => { value.clients.other = { ...value.clients['demo-client'] }; }],
  ['URL credentials', value => { value.upstream_url = 'http://user:pass@example.com/mcp'; }],
]) {
  test(`configuration fails closed: ${name}`, () => {
    const policy = config();
    mutate(policy);
    assert.throws(() => prepareConfig(policy, env));
  });
}
test('missing and weak secrets fail startup', () => {
  assert.throws(() => prepareConfig(config(), {}));
  assert.throws(() => prepareConfig(config(), { MCP_GUARD_DEMO_TOKEN: 'short' }));
});
