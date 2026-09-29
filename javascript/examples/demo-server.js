import { pathToFileURL } from 'node:url';
import Fastify from 'fastify';

export function createDemo() {
  const app = Fastify({ logger: false });
  app.post('/mcp', async (request, reply) => {
    const message = request.body;
    let result;
    switch (message.method) {
      case 'notifications/initialized': return reply.code(202).send();
      case 'initialize':
        result = { protocolVersion: message.params.protocolVersion, capabilities: { tools: {} },
          serverInfo: { name: 'demo-js', version: '1.0.0' } };
        break;
      case 'ping': result = {}; break;
      case 'tools/list':
        result = { tools: [
          { name: 'echo', description: 'Echo text', inputSchema: { type: 'object',
            properties: { text: { type: 'string' } }, required: ['text'] } },
          { name: 'admin_reset', description: 'Demo forbidden tool (no-op)', inputSchema: { type: 'object' } },
        ] };
        break;
      case 'tools/call':
        if (message.params.name === 'echo') result = { content: [{ type: 'text', text: message.params.arguments.text }] };
        break;
    }
    return result ? { jsonrpc: '2.0', id: message.id, result } :
      { jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'Unknown method or tool' } };
  });
  return app;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const app = createDemo();
  await app.listen({ host: '127.0.0.1', port: Number(process.env.PORT ?? 9000) });
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { await app.close(); });
  console.error('Demo MCP server listening on loopback');
}
