import { parseArgs } from 'node:util';
import { createApp } from './app.js';
import { loadConfig } from './config.js';

const { values } = parseArgs({ options: {
  config: { type: 'string', default: 'policy.json' },
  host: { type: 'string', default: '127.0.0.1' },
  port: { type: 'string', default: '8080' },
  help: { type: 'boolean', default: false },
} });
if (values.help) {
  console.log('Usage: npm start -- [--config policy.json] [--host 127.0.0.1] [--port 8080]');
} else {
  const port = Number(values.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port');
  const app = createApp(loadConfig(values.config));
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { await app.close(); });
  await app.listen({ port, host: values.host });
  console.error(`MCP Guard JavaScript listening on ${values.host}:${port}`);
}
