import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createApp } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { sendUpstream } from '../src/upstream.js';
import { createDemo } from './demo-server.js';

const token = randomBytes(32).toString('hex');
const demo = createDemo();
let upstreamCalls = 0;
demo.addHook('onRequest', async () => { upstreamCalls++; });
let gateway;
const probe = createServer((request, response) => {
  if (request.url === '/stall') return;
  if (request.url === '/large') {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('x'.repeat(2048));
  } else {
    response.writeHead(302, { location: '/large' });
    response.end();
  }
});
try {
  await demo.listen({ host: '127.0.0.1', port: 0 });
  const policy = loadConfig(new URL('../policy.json', import.meta.url));
  policy.upstream_url = `http://127.0.0.1:${demo.server.address().port}/mcp`;
  gateway = createApp(policy, { env: { MCP_GUARD_DEMO_TOKEN: token }, audit: () => {} });
  await gateway.listen({ host: '127.0.0.1', port: 0 });
  const base = `http://127.0.0.1:${gateway.server.address().port}`;
  const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json',
    accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-11-25' };
  const post = (method, params, id = 1) => fetch(base + '/mcp', { method: 'POST', headers,
    body: JSON.stringify({ jsonrpc: '2.0', ...(id === null ? {} : { id }), method, params }) });
  assert.equal((await fetch(base + '/healthz')).status, 200);
  assert.equal((await post('initialize', { protocolVersion: '2025-11-25', capabilities: {},
    clientInfo: { name: 'smoke', version: '1' } })).status, 200);
  assert.equal((await post('notifications/initialized', {}, null)).status, 202);
  const catalog = await (await post('tools/list', {})).json();
  assert.deepEqual(catalog.result.tools.map(tool => tool.name), ['echo']);
  const response = await post('tools/call', { name: 'echo', arguments: { text: 'socket-smoke' } });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).result.content[0].text, 'socket-smoke');
  const before = upstreamCalls;
  assert.equal((await post('tools/call', { name: 'admin_reset', arguments: {} })).status, 403);
  assert.equal(upstreamCalls, before);

  probe.listen(0, '127.0.0.1');
  await once(probe, 'listening');
  const probeBase = `http://127.0.0.1:${probe.address().port}`;
  const options = { headers: { 'content-type': 'application/json' }, body: '{}', maxBytes: 1024, timeoutMs: 100 };
  await assert.rejects(sendUpstream(probeBase + '/large', options), /size limit/);
  await assert.rejects(sendUpstream(probeBase + '/stall', options), /timeout/);
  const redirect = await sendUpstream(probeBase + '/redirect', options);
  assert.equal(redirect.status, 302);
  console.log('Real HTTP smoke passed: initialization, discovery, allowed/denied calls, size limit, timeout, no redirects.');
} finally {
  await gateway?.close();
  await demo.close();
  probe.closeAllConnections();
  if (probe.listening) await new Promise(resolve => probe.close(resolve));
}
