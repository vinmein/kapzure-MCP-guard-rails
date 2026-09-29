import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from examples.demo_server import app as demo
from mcp_guard.app import create_app
from mcp_guard.config import Rate, Settings
from mcp_guard.policy import Limiter, Rejection

TOKEN = "a" * 40
OTHER_TOKEN = "b" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}", "MCP-Protocol-Version": "2025-11-25",
           "Accept": "application/json, text/event-stream"}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("MCP_GUARD_DEMO_TOKEN", TOKEN)
    monkeypatch.setenv("OTHER_TOKEN", OTHER_TOKEN)
    return json.loads(Path("policy.json").read_text())


def message(method="tools/call", params=None):
    if params is None:
        params = {"name": "echo", "arguments": {"text": "hello"}}
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}


def client(config, handler=None):
    transport = httpx.MockTransport(handler) if handler else httpx.ASGITransport(app=demo)
    return TestClient(create_app(Settings.model_validate(config), transport))


def test_end_to_end(config):
    with client(config) as gateway:
        init = gateway.post("/mcp", headers=HEADERS, json=message("initialize", {
            "protocolVersion": "2025-11-25", "clientInfo": {"name": "test", "version": "1"},
            "capabilities": {"sampling": {}},
        }))
        assert init.status_code == 200
        assert init.json()["result"]["capabilities"] == {"tools": {}}
        ready = gateway.post("/mcp", headers=HEADERS, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        assert ready.status_code == 202 and not ready.content
        catalog = gateway.post("/mcp", headers=HEADERS, json=message("tools/list", {}))
        assert [t["name"] for t in catalog.json()["result"]["tools"]] == ["echo"]
        assert catalog.json()["result"]["tools"][0]["inputSchema"]["additionalProperties"] is False
        response = gateway.post("/mcp", headers=HEADERS, json=message())
        assert response.status_code == 200
        assert response.json()["result"]["content"][0]["text"] == "hello"
        assert gateway.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("payload,status", [
    (message(params={"name": "admin_reset", "arguments": {}}), 403),
    (message(params={"name": "echo", "arguments": {"text": 9}}), 400),
    (message(params={"name": "echo", "arguments": {"text": "ok", "extra": 1}}), 400),
    (message(params={"name": "echo", "arguments": {"text": "demo_blocked_TEXT"}}), 403),
    (message(params={"name": "echo", "arguments": {"text": "ok"}, "task": {}}), 400),
    (message("resources/read", {"uri": "file:///secret"}), 403),
    (message("tools/list", {"_meta": {}}), 400),
    (message("initialize", {"protocolVersion": []}), 400),
    ([message(), message()], 400),
    ({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "echo"}}, 400),
    ({"jsonrpc": "2.0", "id": True, "method": "ping"}, 400),
])
def test_rejections_never_reach_upstream(config, payload, status):
    calls = []
    def upstream(request):
        calls.append(request)
        raise AssertionError("Blocked request reached upstream")
    with client(config, upstream) as gateway:
        response = gateway.post("/mcp", headers=HEADERS, json=payload)
    assert response.status_code == status
    assert calls == []


def test_auth_origin_session_and_transport(config):
    with client(config, lambda _: pytest.fail("Must not forward")) as gateway:
        assert gateway.post("/mcp", json=message()).status_code == 401
        assert gateway.get("/mcp").status_code == 401
        assert gateway.get("/mcp", headers=HEADERS).status_code == 405
        for extra, status in [
            ({"Origin": "https://evil.example"}, 403),
            ({"MCP-Session-ID": "another-client-session"}, 400),
            ({"MCP-Protocol-Version": "unknown"}, 400),
            ({"Content-Encoding": "gzip"}, 415),
        ]:
            assert gateway.post("/mcp", headers=HEADERS | extra, json=message()).status_code == status


@pytest.mark.parametrize("raw", [
    '{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/call"}',
    '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1e999}}',
    r'{"jsonrpc":"2.0","id":"\ud800","method":"ping"}',
    '{bad',
    '[[[[',
])
def test_invalid_json(config, raw):
    with client(config, lambda _: pytest.fail("Must not forward")) as gateway:
        response = gateway.post("/mcp", headers=HEADERS | {"Content-Type": "application/json"}, content=raw)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32700


def test_body_and_depth_limits(config):
    config["max_request_bytes"] = 1024
    config["max_json_depth"] = 4
    with client(config) as gateway:
        assert gateway.post("/mcp", headers=HEADERS, json=message(params={
            "name": "echo", "arguments": {"text": "x" * 2000}})).status_code == 413
        assert gateway.post("/mcp", headers=HEADERS, json=message(params={
            "name": "echo", "arguments": {"a": {"b": {"c": 1}}}})).status_code == 400


def test_client_limits_and_isolation(config):
    config["clients"]["demo-client"]["rate_limit"]["requests"] = 1
    config["clients"]["other"] = {"token_env": "OTHER_TOKEN", "allowed_tools": []}
    with client(config) as gateway:
        assert gateway.post("/mcp", headers=HEADERS, json=message()).status_code == 200
        limited = gateway.post("/mcp", headers=HEADERS, json=message())
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1
        other = HEADERS | {"Authorization": f"Bearer {OTHER_TOKEN}"}
        catalog = gateway.post("/mcp", headers=other, json=message("tools/list", {}))
        assert catalog.json()["result"]["tools"] == []
        assert gateway.post("/mcp", headers=other, json=message()).status_code == 403


def test_tool_limit_does_not_block_ping(config):
    config["tools"]["echo"]["rate_limit"]["requests"] = 1
    with client(config) as gateway:
        assert gateway.post("/mcp", headers=HEADERS, json=message()).status_code == 200
        assert gateway.post("/mcp", headers=HEADERS, json=message()).status_code == 429
        assert gateway.post("/mcp", headers=HEADERS, json=message("ping", {})).status_code == 200


def test_sliding_window_expiry():
    now = [0.0]
    limiter = Limiter(lambda: now[0])
    rate = Rate(requests=1, window_seconds=10)
    limiter.consume("a", "all", rate)
    now[0] = 9.2
    with pytest.raises(Rejection) as failure:
        limiter.consume("a", "all", rate)
    assert failure.value.retry_after == 1
    now[0] = 10
    limiter.consume("a", "all", rate)


def test_credentials_are_not_forwarded(config, monkeypatch):
    monkeypatch.setenv("UPSTREAM_TOKEN", "upstream-private-token")
    config["upstream_token_env"] = "UPSTREAM_TOKEN"
    def upstream(request):
        assert request.headers["authorization"] == "Bearer upstream-private-token"
        assert "cookie" not in request.headers
        assert "x-forwarded-for" not in request.headers
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {}})
    with client(config, upstream) as gateway:
        assert gateway.post("/mcp", json=message("ping", {}), headers=HEADERS | {
            "Cookie": "secret=yes", "X-Forwarded-For": "admin"}).status_code == 200


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "http://attacker.example"}),
    httpx.Response(200, text="data: {}\n\n", headers={"content-type": "text/event-stream"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {}}, headers={"mcp-session-id": "session"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 8, "result": {}}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {}, "error": {}}),
    httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"x": "a" * 2000}}),
])
def test_upstream_failures(config, response):
    config["max_response_bytes"] = 1024
    with client(config, lambda _: response) as gateway:
        assert gateway.post("/mcp", headers=HEADERS, json=message()).status_code == 502


def test_timeout_is_not_retried(config):
    calls = []
    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("internal secret")
    with client(config, upstream) as gateway:
        response = gateway.post("/mcp", headers=HEADERS, json=message())
    assert response.status_code == 502 and len(calls) == 1
    assert "internal secret" not in response.text


def test_audit_does_not_log_arguments_or_tokens(config, caplog):
    caplog.set_level(logging.INFO, logger="mcp_guard.audit")
    with client(config) as gateway:
        gateway.post("/mcp", headers=HEADERS, json=message(params={
            "name": "echo", "arguments": {"text": "sensitive-argument"}}))
    records = [json.loads(record.message) for record in caplog.records if record.name == "mcp_guard.audit"]
    assert records[0]["client"] == "demo-client"
    assert "sensitive-argument" not in caplog.text and TOKEN not in caplog.text


def test_config_fails_closed(config, monkeypatch):
    config["clients"]["demo-client"]["allowed_tools"] = ["missing"]
    with pytest.raises(ValueError):
        Settings.model_validate(config)
    config["clients"]["demo-client"]["allowed_tools"] = ["echo"]
    monkeypatch.delenv("MCP_GUARD_DEMO_TOKEN")
    with pytest.raises(ValueError):
        create_app(Settings.model_validate(config))
    config["tools"]["echo"]["input_schema"]["$ref"] = "https://attacker.example/schema"
    with pytest.raises(ValueError):
        Settings.model_validate(config)


def test_in_flight_capacity_and_recovery(config):
    config["max_in_flight"] = 1
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def upstream(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {}})
        app = create_app(Settings.model_validate(config), httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as gateway:
                first = asyncio.create_task(gateway.post("/mcp", headers=HEADERS, json=message()))
                await asyncio.wait_for(entered.wait(), 1)
                second = await gateway.post("/mcp", headers=HEADERS, json=message())
                assert second.status_code == 503
                release.set()
                assert (await first).status_code == 200
                assert (await gateway.post("/mcp", headers=HEADERS, json=message())).status_code == 200
    asyncio.run(scenario())


def test_total_timeout_releases_capacity(config):
    config["timeout_seconds"] = 0.01
    async def upstream(request):
        await asyncio.sleep(1)
        pytest.fail("Upstream operation should be cancelled")
    with client(config, upstream) as gateway:
        assert gateway.post("/mcp", headers=HEADERS, json=message()).status_code == 502
        assert gateway.app.state.in_flight == 0
