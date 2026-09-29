"""Local stateless MCP test server. Bind to loopback only; not a production server."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()


@app.post("/mcp")
async def mcp(request: Request):
    message = await request.json()
    method = message["method"]
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        result = {"protocolVersion": message["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "demo", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "echo", "description": "Echo text", "inputSchema": {
                "type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"]}},
            {"name": "admin_reset", "description": "Demo forbidden tool (no-op)",
             "inputSchema": {"type": "object"}},
        ]}
    elif method == "tools/call" and message["params"]["name"] == "echo":
        result = {"content": [{"type": "text", "text": message["params"]["arguments"]["text"]}]}
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"),
                             "error": {"code": -32601, "message": "Unknown method or tool"}})
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}
