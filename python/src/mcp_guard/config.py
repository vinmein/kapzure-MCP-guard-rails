"""Validated, operator-owned policy. Secrets are resolved from the environment."""

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rate(StrictModel):
    requests: int = Field(default=60, ge=1, le=100000)
    window_seconds: int = Field(default=60, ge=1, le=86400)


class Tool(StrictModel):
    input_schema: dict[str, Any]
    blocked_substrings: list[str] = Field(default_factory=list)
    rate_limit: Rate | None = None

    @model_validator(mode="after")
    def validate_schema(self):
        Draft202012Validator.check_schema(self.input_schema)
        if self.input_schema.get("type") != "object":
            raise ValueError("Tool input_schema must have type object")
        # Do not allow schema validation to fetch URLs or resolve external files.
        def check(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef"}:
                        raise ValueError("Schema references are unsupported; inline the schema")
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(self.input_schema)
        if any(not value for value in self.blocked_substrings):
            raise ValueError("Blocked substrings must not be empty")
        return self


class Client(StrictModel):
    token_env: str = Field(min_length=1)
    allowed_tools: list[str]
    rate_limit: Rate = Field(default_factory=Rate)


class Settings(StrictModel):
    upstream_url: str
    upstream_token_env: str | None = None
    allowed_origins: list[str] = Field(default_factory=list)
    max_request_bytes: int = Field(default=65536, ge=1024, le=10485760)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=104857600)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_in_flight: int = Field(default=32, ge=1, le=1000)
    max_json_depth: int = Field(default=32, ge=1, le=64)
    clients: dict[str, Client] = Field(min_length=1)
    tools: dict[str, Tool]

    @model_validator(mode="after")
    def validate_policy(self):
        url = urlsplit(self.upstream_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("upstream_url must be an HTTP(S) URL")
        if url.username or url.password or url.fragment or url.query:
            raise ValueError("Use upstream_token_env, not URL credentials, query, or fragment")
        for client in self.clients.values():
            if set(client.allowed_tools) - self.tools.keys():
                raise ValueError("Client allowlist refers to an undefined tool")
        return self

    def credentials(self) -> dict[str, bytes]:
        result = {}
        for name, client in self.clients.items():
            value = required_secret(client.token_env)
            if len(value) < 32:
                raise ValueError(f"{client.token_env} must contain at least 32 characters")
            token = value.encode("utf-8")
            if token in result.values():
                raise ValueError("Client tokens must be distinct")
            result[name] = token
        return result


def required_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"Set {name} to a nonempty token without whitespace")
    return value


def load_settings(path: str | Path) -> Settings:
    return Settings.model_validate(json.loads(Path(path).read_text()))
