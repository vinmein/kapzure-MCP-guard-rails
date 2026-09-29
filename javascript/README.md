# MCP Guard — JavaScript

A standalone Node.js service that validates and authorizes MCP calls before forwarding them to one configured upstream server. This project runs independently of the Python implementation.

## Run

Requires **Node.js 22 or later** and npm. From this folder:

```sh
npm ci
export MCP_GUARD_DEMO_TOKEN="$(node --input-type=module -e "import { randomBytes } from 'node:crypto'; console.log(randomBytes(32).toString('hex'))")"
npm start
```

Start the included upstream in another terminal, also from this folder:

```sh
npm run demo
```

The gateway listens on `http://127.0.0.1:8080/mcp`; the demo server listens on port 9000. Connect an MCP client using Streamable HTTP with `Authorization: Bearer <your token>`. The demo does not need the token. Keep the generated token in the gateway terminal to run this example:

```sh
curl --fail-with-body http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer $MCP_GUARD_DEMO_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}'
```

Normal MCP clients initialize before calling tools. Change the tool to `admin_reset` for a 403 denial, or change the text to `DEMO_BLOCKED_TEXT` to trigger the sample content rule. The forbidden demo tool performs no administrative action.

Custom startup:

```sh
npm start -- --config policy.json --host 127.0.0.1 --port 8081
```

The programmatic entry point is `createApp(config)` from `src/app.js`; it returns a Fastify instance. Await `app.listen({ host, port })` to start it and `app.close()` to stop it.

## Configure guardrails

Edit `policy.json`, then restart the gateway. Secrets are environment variable references; no tokens belong in policy files.

| Setting | Behavior |
| --- | --- |
| `upstream_url` | One fixed HTTP(S) destination. URL credentials, query strings, fragments, redirects, and environment proxies are not supported. |
| `upstream_token_env` | Optional environment variable containing a separate upstream bearer token. Client credentials, cookies, and arbitrary headers are never forwarded. |
| `clients.<name>.token_env` | Environment variable containing this client's random secret, at least 32 characters. Duplicate client tokens fail startup. |
| `clients.<name>.allowed_tools` | Exact names the client may discover and call; no wildcards. Empty means no tools. |
| `clients.<name>.rate_limit` | Sliding window `{ "requests": 60, "window_seconds": 60 }`, covering all authenticated requests, including rejected ones. |
| `tools.<name>.input_schema` | Operator-owned JSON Schema Draft 2020-12. Set required fields, length limits, enums, and `additionalProperties: false`. References are disabled. |
| `tools.<name>.blocked_substrings` | Optional case-insensitive literal matches against argument keys and string values, including nested values. |
| `tools.<name>.rate_limit` | Optional per-client, per-tool window using the same rate structure. |
| `allowed_origins` | Exact browser Origins permitted. The default rejects requests with an Origin header; authenticated non-browser requests without one are accepted. |
| `max_request_bytes`, `max_response_bytes` | Request and upstream response limits; defaults 64 KiB and 1 MiB. Compressed payloads are rejected. |
| `max_json_depth` | Nesting limit, default 32. |
| `timeout_seconds` | Body-read and complete upstream-operation timeout, default 30 seconds. |
| `max_in_flight` | Maximum active upstream calls, default 32. |

Tool discovery hides forbidden tools and exposes configured input schemas. Pagination cursors are preserved even when filtering leaves a page empty. Every call is authorized independently of discovery.

Configuration rejects unknown settings, invalid schemas, undefined allowlist tools, missing/weak credentials, and external schema references. [Ajv](https://ajv.js.org/json-schema) validates Draft 2020-12 schemas, with [ajv-formats](https://ajv.js.org/guide/formats) for standard formats. Strict compilation rejects unknown schema keywords and formats. Input values are not coerced, defaulted, or stripped.

JSON parsing rejects duplicate keys (including escaped equivalents), invalid UTF-8, lone surrogates, non-finite numbers, and integer values outside JavaScript's safe range. Use strings for identifiers larger than `Number.MAX_SAFE_INTEGER`. Non-integer numbers retain JavaScript's standard floating-point behavior. Literal checks use `toLowerCase()`; Python's Unicode case folding can differ for non-ASCII text.

## Transport scope

Supports **stateless MCP Streamable HTTP with JSON responses**, for protocol versions `2025-03-26`, `2025-06-18`, and `2025-11-25`. See the [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports). Newer versions fail closed until implemented.

Supported methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call`. Initialization advertises only tools and removes client callback capabilities. All other MCP methods are denied. Non-POST `/mcp` requests return 405 after authentication.

The upstream must run in stateless JSON response mode. Stateful sessions, SSE, stdio, stream resumption, sampling, elicitation, progress/cancellation, resources, prompts, tasks, and OAuth discovery are not implemented. Tool `_meta` and task parameters are rejected. No stored initialization session is required.

## Errors and logs

Policy errors use JSON-RPC envelopes and retain a safe request ID. Responses carry a gateway-generated `X-Request-ID`. HTTP statuses include 400 for invalid input, 401 for authentication, 403 for denied access, 408 for body-read timeout, 413 for oversized input, 415 for unsupported media/encoding, 429 for quotas, 503 for capacity, and 502 for upstream failures. Rate and capacity responses include `Retry-After`.

One JSON audit record per `/mcp` response includes configured client identity, recognized method, allowed tool name, status, reason, trace ID, and duration. Credentials, arguments, results, and client-provided request IDs are omitted. Request logging is disabled in Fastify. `GET /healthz` is unauthenticated process liveness, not an upstream health check.

Upstream JSON-RPC errors retain their error code with a generic message. Calls are never retried automatically: an upstream failure or timeout may happen **after execution**, so verify mutating operations before retrying them.

## Deployment boundaries

- Restrict direct access to the upstream using network rules and/or separate credentials. Direct requests bypass the gateway.
- Use TLS at ingress for remote deployments. The default listener is loopback only.
- Run one process/worker/replica for the in-memory quotas. Restarts clear limits; use a shared limiter such as Redis before scaling across processes.
- Apply unauthenticated traffic and connection limits at ingress. These application quotas identify authenticated clients.
- Keep schemas operator-controlled. Expensive regex or combinatorial rules can block Node's event loop; timeouts do not interrupt synchronous schema validation.
- The upstream remains responsible for data authorization and validation. This gateway does not sandbox tools, constrain their network/filesystem access, scan outputs for secrets, or detect prompt injection and poisoned tool descriptions.

## Verify

```sh
npm test
npm run test:smoke
```

The unit/integration suite uses Node's built-in test runner and [Fastify injection](https://fastify.dev/docs/latest/Guides/Testing/), without opening sockets. The smoke test starts temporary loopback servers and exercises the actual HTTP transport, including rejected calls, response limits, timeouts, and redirect handling. It closes all servers when finished.
