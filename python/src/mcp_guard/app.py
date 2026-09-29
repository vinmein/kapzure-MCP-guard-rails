"""Stateless, JSON-response MCP HTTP gateway; unsupported features fail closed."""

import asyncio
import hmac
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import Settings, required_secret
from .policy import Limiter, Policy, Rejection, check_depth

VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}
METHODS = {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"}
audit = logging.getLogger("mcp_guard.audit")


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON number")

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number")
        return parsed

    result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                        parse_constant=constant, parse_float=number)
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            value.encode("utf-8")  # Reject lone surrogates before forwarding or responding.
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return result


def validate_message(value, settings: Settings):
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise Rejection(400, -32600, "Expected one JSON-RPC 2.0 message")
    if set(value) - {"jsonrpc", "id", "method", "params"}:
        raise Rejection(400, -32600, "Unsupported JSON-RPC fields")
    if "id" in value and (type(value["id"]) not in {str, int}):
        raise Rejection(400, -32600, "Request ID must be a string or integer")
    if not isinstance(value.get("method"), str):
        raise Rejection(400, -32600, "Method must be a string")
    if value["method"] not in METHODS:
        raise Rejection(403, -32601, "Method is not permitted")
    notification = value["method"] == "notifications/initialized"
    if notification == ("id" in value):
        raise Rejection(400, -32600, "Invalid request or notification ID")
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise Rejection(400, -32602, "Params must be an object")
    check_depth(value, settings.max_json_depth)
    return params


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    credentials = settings.credentials()
    upstream_token = required_secret(settings.upstream_token_env) if settings.upstream_token_env else None
    policy = Policy(settings)
    limiter = Limiter()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, timeout=settings.timeout_seconds,
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=settings.max_in_flight),
        ) as client:
            app.state.upstream = client
            app.state.in_flight = 0
            yield

    app = FastAPI(title="MCP Guard", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    async def forward(message: dict, version: str):
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "accept-encoding": "identity",
            "mcp-protocol-version": version,
        }
        if upstream_token:
            headers["authorization"] = f"Bearer {upstream_token}"
        # No client credentials, cookies, forwarding headers, or arbitrary headers pass through.
        async with app.state.upstream.stream(
            "POST", settings.upstream_url, json=message, headers=headers,
        ) as response:
            if not 200 <= response.status_code < 300:
                raise Rejection(502, -32002, "Upstream rejected the request")
            if "mcp-session-id" in response.headers:
                raise Rejection(502, -32002, "Upstream must use stateless HTTP mode")
            if "id" not in message:
                if response.status_code != 202:
                    raise Rejection(502, -32002, "Invalid upstream notification response")
                return None
            if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise Rejection(502, -32002, "Upstream must use JSON response mode")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise Rejection(502, -32002, "Compressed upstream responses are unsupported")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > settings.max_response_bytes:
                    raise Rejection(502, -32002, "Upstream response exceeds size limit")
            try:
                value = strict_json(bytes(raw))
                check_depth(value, settings.max_json_depth)
                valid = (
                    isinstance(value, dict) and value.get("jsonrpc") == "2.0"
                    and type(value.get("id")) is type(message["id"])
                    and value.get("id") == message["id"]
                    and (set(value) == {"jsonrpc", "id", "result"}
                         or set(value) == {"jsonrpc", "id", "error"})
                )
                if not valid:
                    raise ValueError("Invalid upstream envelope")
                if "error" in value:
                    error = value["error"]
                    if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                        raise ValueError("Invalid upstream error")
                    # Upstream errors may contain implementation details or secrets.
                    value["error"] = {"code": error["code"], "message": "Upstream MCP error"}
                elif not isinstance(value["result"], dict):
                    raise ValueError("Invalid upstream result")
            except (ValueError, RecursionError, Rejection):
                raise Rejection(502, -32002, "Invalid upstream JSON-RPC response") from None
            return value

    @app.api_route("/mcp", methods=["POST", "GET", "DELETE"])
    async def mcp(request: Request):
        started = time.monotonic()
        trace_id = uuid.uuid4().hex
        client_name = None
        method = None
        tool_name = None
        request_id = None
        status = 500
        reason = "Internal error"
        try:
            origin = request.headers.get("origin")
            if origin is not None and origin not in settings.allowed_origins:
                raise Rejection(403, -32003, "Origin is not allowed")
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                token_bytes = token.encode("utf-8")
                for name, expected in credentials.items():
                    if hmac.compare_digest(token_bytes, expected):
                        client_name = name
            if client_name is None:
                raise Rejection(401, -32001, "Valid bearer authentication is required")
            limiter.consume(client_name, "all", settings.clients[client_name].rate_limit)
            if request.method != "POST":
                raise Rejection(405, -32000, "Only POST is supported in stateless JSON mode")
            if "mcp-session-id" in request.headers or "last-event-id" in request.headers:
                raise Rejection(400, -32600, "Sessions and stream resumption are unsupported")
            version = request.headers.get("mcp-protocol-version", "2025-03-26")
            if version not in VERSIONS:
                raise Rejection(400, -32600, "Unsupported MCP protocol version")
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise Rejection(415, -32600, "Content-Type must be application/json")
            if request.headers.get("content-encoding", "identity") != "identity":
                raise Rejection(415, -32600, "Compressed requests are unsupported")
            raw = bytearray()
            async with asyncio.timeout(settings.timeout_seconds):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > settings.max_request_bytes:
                        raise Rejection(413, -32600, "Request exceeds size limit")
            try:
                message = strict_json(bytes(raw))
            except (ValueError, RecursionError):
                raise Rejection(400, -32700, "Invalid JSON") from None
            if isinstance(message, dict) and type(message.get("id")) in {str, int}:
                request_id = message["id"]
            params = validate_message(message, settings)
            request_id = message.get("id")
            method = message["method"]
            if method == "tools/call":
                tool_name = policy.check_tool(client_name, params)
                rule = settings.tools[tool_name].rate_limit
                if rule:
                    limiter.consume(client_name, "tool:" + tool_name, rule)
            elif method == "initialize":
                if (not isinstance(params.get("protocolVersion"), str)
                    or params["protocolVersion"] not in VERSIONS
                    or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(params.get("clientInfo"), dict)
                    or not isinstance(params["clientInfo"].get("name"), str)
                    or not isinstance(params["clientInfo"].get("version"), str)):
                    raise Rejection(400, -32602, "Invalid or unsupported initialization parameters")
                version = params["protocolVersion"]
                message["params"] = {"protocolVersion": version, "capabilities": {},
                                     "clientInfo": {"name": "mcp-guard", "version": "0.1.0"}}
            elif method == "tools/list":
                if set(params) - {"cursor"} or ("cursor" in params and not isinstance(params["cursor"], str)):
                    raise Rejection(400, -32602, "Unsupported tools/list parameters")
            elif params:
                raise Rejection(400, -32602, "This method takes no parameters")
            if app.state.in_flight >= settings.max_in_flight:
                raise Rejection(503, -32000, "Gateway is at capacity", 1)
            app.state.in_flight += 1
            try:
                async with asyncio.timeout(settings.timeout_seconds):
                    value = await forward(message, version)
            finally:
                app.state.in_flight -= 1
            if value is not None and "result" in value:
                result = value["result"]
                if method == "tools/list":
                    entries = result.get("tools")
                    if not isinstance(entries, list) or any(
                        not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                        for entry in entries
                    ):
                        raise Rejection(502, -32002, "Invalid upstream tool catalog")
                    allowed = settings.clients[client_name].allowed_tools
                    visible = []
                    for entry in entries:
                        if entry["name"] in allowed:
                            entry = dict(entry)
                            entry["inputSchema"] = settings.tools[entry["name"]].input_schema
                            visible.append(entry)
                    value["result"] = {"tools": visible}
                    if "nextCursor" in result:
                        if not isinstance(result["nextCursor"], str):
                            raise Rejection(502, -32002, "Invalid upstream pagination cursor")
                        value["result"]["nextCursor"] = result["nextCursor"]
                elif method == "initialize":
                    if (not isinstance(result.get("protocolVersion"), str)
                        or result["protocolVersion"] not in VERSIONS):
                        raise Rejection(502, -32002, "Unsupported upstream protocol version")
                    value["result"] = {
                        "protocolVersion": result["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mcp-guard", "version": "0.1.0"},
                    }
            status = 202 if value is None else 200
            reason = "Upstream MCP error" if value and "error" in value else "Allowed"
            if value is None:
                return Response(status_code=202, headers={"X-Request-ID": trace_id})
            return JSONResponse(value, headers={"X-Request-ID": trace_id, "Cache-Control": "no-store"})
        except (httpx.HTTPError, TimeoutError):
            status, reason = 502, "Upstream or request transport failed"
            return error_response(status, -32002, reason, request_id, trace_id)
        except Rejection as exc:
            status, reason = exc.status, exc.reason
            return error_response(status, exc.code, reason, request_id, trace_id, exc.retry_after)
        finally:
            audit.info(json.dumps({
                "event": "mcp_request", "trace_id": trace_id, "client": client_name,
                "method": method, "tool": tool_name, "status": status, "reason": reason,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }))

    return app


def error_response(status, code, reason, request_id, trace_id, retry_after=None):
    headers = {"X-Request-ID": trace_id, "Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status == 405:
        headers["Allow"] = "POST"
    return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": code, "message": reason}},
                        status_code=status, headers=headers)
