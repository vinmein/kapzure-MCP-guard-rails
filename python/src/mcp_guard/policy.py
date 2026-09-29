"""Pure policy checks and a process-local sliding-window limiter."""

import math
import time
from collections import deque
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

from .config import Rate, Settings


class Rejection(Exception):
    def __init__(self, status: int, code: int, reason: str, retry_after: int | None = None):
        self.status = status
        self.code = code
        self.reason = reason
        self.retry_after = retry_after


class Limiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.windows: dict[tuple[str, str], deque[float]] = {}

    def consume(self, client: str, scope: str, rate: Rate):
        # No await between check and append: atomic within the single event loop.
        now = self.clock()
        window = self.windows.setdefault((client, scope), deque())
        while window and window[0] <= now - rate.window_seconds:
            window.popleft()
        if len(window) >= rate.requests:
            retry = max(1, math.ceil(window[0] + rate.window_seconds - now))
            raise Rejection(429, -32029, "Rate limit exceeded", retry)
        window.append(now)


def check_depth(value: Any, maximum: int):
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > maximum:
            raise Rejection(400, -32602, "JSON nesting limit exceeded")
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)


class Policy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.validators = {
            name: Draft202012Validator(tool.input_schema, format_checker=FormatChecker())
            for name, tool in settings.tools.items()
        }

    def check_tool(self, client: str, params: dict) -> str:
        if set(params) - {"name", "arguments"}:
            raise Rejection(400, -32602, "Unsupported tool call parameters")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise Rejection(400, -32602, "Tool name is required")
        if name not in self.settings.clients[client].allowed_tools:
            raise Rejection(403, -32003, "Tool is not permitted")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise Rejection(400, -32602, "Tool arguments must be an object")
        if not self.validators[name].is_valid(arguments):
            # Validation messages can contain secret argument values.
            raise Rejection(400, -32602, "Tool arguments violate the configured schema")
        blocked = [word.casefold() for word in self.settings.tools[name].blocked_substrings]
        pending = [arguments]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str) and any(word in value.casefold() for word in blocked):
                raise Rejection(403, -32003, "Tool arguments contain prohibited text")
        return name
