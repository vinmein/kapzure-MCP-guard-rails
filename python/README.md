# MCP Guard

A standalone Python service that checks MCP tool calls before forwarding them to one configured upstream server.

```text
MCP client → Bearer authentication → Client rate limit → Method/tool allowlist
           → Input schema + text checks → Tool rate limit → Upstream MCP server
```

## Run locally

Requires Python 3.11 or later. From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export MCP_GUARD_DEMO_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn examples.demo_server:app --host 127.0.0.1 --port 9000
```

In another terminal, activate the same virtual environment, export the **same** token value, then start the gateway:

```sh
mcp-guard --config policy.json --port 8080
```

Connect an MCP client to `http://127.0.0.1:8080/mcp` using the header `Authorization: Bearer <your token>` and Streamable HTTP transport. The token is resolved once at startup; restart after rotating it or editing policy. The following request illustrates a tool call (normal MCP clients initialize first):

```sh
curl --fail-with-body http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer $MCP_GUARD_DEMO_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}'
```

Change the name to `admin_reset` to see a 403 denial. Set text to `DEMO_BLOCKED_TEXT` to trigger a content rule. The demo server advertises `admin_reset`, but the gateway hides it from `tools/list` and prevents direct calls. The demo tool is a no-op; it performs no administrative action.

## Policy

Edit `policy.json` to set your upstream URL and define tools and clients. Configuration rejects unknown keys, unknown tool references, invalid schemas, missing credentials, and duplicate client tokens at startup.

| Setting | Behavior |
| --- | --- |
| `upstream_url` | Fixed operator-controlled HTTP(S) endpoint; request bodies cannot select a destination. Redirects and environment proxies are disabled. |
| `upstream_token_env` | Optional environment variable with a separate upstream bearer token. Client credentials and cookies are never forwarded. |
| `clients.<name>.token_env` | Environment variable holding this client's secret, at least 32 characters. Use a cryptographically random token. |
| `clients.<name>.allowed_tools` | Exact tool names this client may discover and call. Empty means no tools. |
| `clients.<name>.rate_limit` | Sliding window for all authenticated requests, including denied requests. |
| `tools.<name>.input_schema` | Operator-owned JSON Schema (Draft 2020-12). Use required fields, enums, length limits, and `additionalProperties: false`. Schema references are disabled to prevent external resolution. |
| `tools.<name>.blocked_substrings` | Optional case-insensitive checks across argument keys and string values. These are literal checks, not a prompt injection detector. |
| `tools.<name>.rate_limit` | Optional per-client, per-tool sliding window. |
| `allowed_origins` | Exact permitted browser Origins. By default requests with an Origin header are denied; non-browser clients without one are accepted with authentication. |
| `max_request_bytes`, `max_response_bytes` | Bounded request and upstream response bodies. Compressed payloads are rejected. |
| `max_json_depth` | Maximum accepted nesting depth, default 32. |
| `timeout_seconds`, `max_in_flight` | Bounded request reading and upstream operation time; maximum concurrent upstream operations. |

The effective permission is the intersection of the configured tool definitions and the client's allowlist. There are no wildcard grants or implicit administrator privileges. Input validation uses configured schemas even if the upstream catalog changes. Discovery exposes those configured schemas; pagination cursors are preserved, including on pages where no tools are visible.

## Transport scope

This initial version supports **stateless MCP Streamable HTTP with JSON responses**, for protocol revisions `2025-03-26`, `2025-06-18`, and `2025-11-25`. See the [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports). Later revisions are explicitly rejected until implemented and tested.

Use an upstream configured for stateless operation and JSON response mode (for example, Python FastMCP's `stateless_http=True, json_response=True`). The included demo implements this subset without requiring an MCP SDK dependency.

Supported methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call`. Initialization removes client capabilities that could cause callbacks and advertises only tools. All other methods are denied. `GET /mcp` and `DELETE /mcp` return 405 after authentication.

Not implemented: stdio, session routing, SSE responses or resumability, sampling, elicitation, progress/cancellation, resources, prompts, tasks, OAuth discovery, and dynamic policy reload. Tool call `_meta` and task parameters are rejected. Configure clients for static bearer authentication. Stateless operation does not require a stored initialization session.

## Errors and audit

Errors use a JSON-RPC envelope with the request ID when safely available and an `X-Request-ID` trace identifier. Authentication failures return 401; policy denials 403; malformed input 400; oversized input 413; unsupported encoding 415; rate limits 429 with `Retry-After`; capacity exhaustion 503; upstream failures 502. Upstream JSON-RPC errors retain their error code with a generic message.

Each `/mcp` request emits one JSON audit log with trace ID, configured client name, recognized method, permitted tool name, status, reason, and duration. Arguments, tool results, authorization headers, and client-supplied request IDs are not logged. Uvicorn access logs are disabled by the CLI. `/healthz` is unauthenticated and reports process liveness only.

The gateway never retries a forwarded call. A timeout, invalid upstream response, or response-size failure can happen **after a tool has executed**. Do not automatically retry mutating calls; verify their outcome at the upstream first.

## Deployment boundaries

- Keep the upstream reachable only from the gateway, using network policy and/or separate upstream credentials. Direct access bypasses these checks.
- Terminate TLS before exposing the gateway remotely. The default listener binds to loopback.
- Run one worker/replica for these in-memory rate limits. Restarts reset counters; multiple workers require a shared limiter such as Redis before using quotas across them.
- Put unauthenticated traffic limits, connection limits, and header/body limits at your ingress. Application rate limits apply to authenticated clients.
- The upstream still enforces data authorization and tool-side validation. The gateway does not sandbox execution, inspect filesystem paths, prevent arbitrary egress inside tools, scan output for secrets, or establish that natural-language content is safe.
- Schemas are trusted operator configuration. Avoid expensive regex patterns and combinatorial validation rules. Review upstream descriptions and tool results before exposing them to a model; this version does not detect tool-description poisoning or prompt injection.

## Verify

```sh
python -m pytest -q
```

Tests cover a complete initialization/discovery/call flow against the demo ASGI server, prevention of forwarding blocked calls, client isolation, tool and client quotas, quota expiry, argument and envelope validation, Origin checks, secret handling, and upstream failure behavior.
