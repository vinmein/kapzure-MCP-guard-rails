# MCP guardrail gateways

Two independent projects implement the same deny-by-default MCP gateway:

| Project | Stack | Setup |
| --- | --- | --- |
| `python/` | Python 3.11+, FastAPI, jsonschema | [Python instructions](python/README.md) |
| `javascript/` | Node.js 22+, JavaScript ES modules, Fastify, Ajv | [JavaScript instructions](javascript/README.md) |

Each folder contains its own dependencies, policy file, source code, demo MCP server, tests, and documentation. Either folder can be copied and run on its own. The original Python implementation now lives in `python/`.

Both gateways provide bearer authentication, exact per-client tool allowlists, filtered tool discovery, JSON Schema input validation, literal blocked-text checks, per-client and per-tool rate limits, size and concurrency limits, and audit logs that omit credentials and arguments.

Both target **stateless MCP Streamable HTTP with JSON responses** and support protocol versions `2025-03-26`, `2025-06-18`, and `2025-11-25`. Stateful sessions, SSE, stdio, OAuth discovery, and model-based prompt injection detection are outside this version's scope. Use one worker per deployment for the in-memory quotas.

## Start a project

For Python, enter `python/` and follow its README. For JavaScript:

```sh
cd javascript
npm ci
export MCP_GUARD_DEMO_TOKEN="$(node --input-type=module -e "import { randomBytes } from 'node:crypto'; console.log(randomBytes(32).toString('hex'))")"
npm start
```

Run `npm run demo` in another terminal inside `javascript/` to start the demo upstream. The gateway listens on `127.0.0.1:8080`, and the demo listens on `127.0.0.1:9000`. Both projects use these defaults; choose different ports and update `upstream_url` if running both at once.

## Tests

```sh
(cd python && python3 -m pytest -q)
(cd javascript && npm test)
(cd javascript && npm run test:smoke)
```

The JavaScript smoke test starts temporary loopback servers on available ports and closes them on completion.

## Policy portability

The included `policy.json` files are identical and share the same field names. Use JSON Schema Draft 2020-12 with inline schemas in either implementation. Ajv uses strict schema compilation, so unknown keywords or formats can reject a configuration that Python accepts. Review complex schemas and regex patterns when moving between language engines.

JavaScript rejects unsafe integer values to avoid silently changing tool arguments or request IDs; represent larger identifiers as strings. Literal text rules use JavaScript lowercase matching and Python Unicode case folding respectively, so non-ASCII matching can differ. Neither implementation treats literal text checks as comprehensive content security.
