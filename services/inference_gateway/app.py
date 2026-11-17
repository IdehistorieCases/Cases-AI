from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

LOCAL_URL = "http://ddu-llm:8080"
LUNA_URL = "http://host.docker.internal:18765"
MAX_BODY = 128_000
MAX_UPSTREAM_RESPONSE = 128_000
MAX_MODEL_RESPONSE = 32_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
MAX_SCHEMA_DEPTH = 16
MAX_SCHEMA_NODES = 2048
MAX_SCHEMA_BYTES = 24_000
MAX_PROMPT_CHARS = 48_000
MAX_ADMITTED_COMPLETIONS = 8
SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PLACEHOLDER_PREFIXES = ("replace-with-", "replace_with_", "change-me", "changeme")


def _is_bounded_json(value: Any, *, max_depth: int, max_nodes: int) -> bool:
    pending = [(value, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if depth > max_depth or visited > max_nodes:
            return False
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    return True


def _valid_response_format(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("type") != "json_schema":
        return False
    definition = value.get("json_schema")
    if not isinstance(definition, dict) or definition.get("strict") is not True:
        return False
    if not isinstance(definition.get("name"), str) or not SCHEMA_NAME.fullmatch(
        definition["name"]
    ):
        return False
    schema = definition.get("schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    try:
        size = len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return False
    return size <= MAX_SCHEMA_BYTES and _is_bounded_json(
        schema, max_depth=MAX_SCHEMA_DEPTH, max_nodes=MAX_SCHEMA_NODES
    )


def _usable_token(token: str) -> bool:
    return len(token) >= 24 and not token.lower().startswith(PLACEHOLDER_PREFIXES)


def client_admin_tokens_valid(client_token: str, admin_token: str) -> bool:
    return _usable_token(client_token) and _usable_token(admin_token) and client_token != admin_token


def bridge_token_valid(client_token: str, admin_token: str, bridge_token: str) -> bool:
    return (
        client_admin_tokens_valid(client_token, admin_token)
        and _usable_token(bridge_token)
        and bridge_token not in {client_token, admin_token}
    )


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    stream: bool = False
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=16)
    response_format: dict[str, Any]
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    temperature: float = Field(default=0.2, ge=0, le=2)
    chat_template_kwargs: dict[str, StrictBool] | None = None

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        total_chars = 0
        for message in messages:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise ValueError("messages must contain bounded text chat messages")
            total_chars += len(message["content"])
        if total_chars > MAX_PROMPT_CHARS:
            raise ValueError("messages exceed the input limit")
        return messages

    @field_validator("response_format")
    @classmethod
    def validate_response_format(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not _valid_response_format(value):
            raise ValueError("a bounded strict object JSON schema is required")
        return value


class ProviderState:
    def __init__(self, path: Path, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(4)
        self.max_admitted_requests = MAX_ADMITTED_COMPLETIONS
        self.admitted_requests = 0
        self.counters: Counter[str] = Counter()
        self.failures = {"local_qwen": 0, "codex_cli": 0}
        self.last_failure = {"local_qwen": None, "codex_cli": None}
        self.last_health_check = {"local_qwen": None, "codex_cli": None}
        self.health_latency_ms = {"local_qwen": None, "codex_cli": None}
        self.last_latency_ms = {"local_qwen": None, "codex_cli": None}
        self.total_latency_ms: Counter[str] = Counter()
        self.latency_samples: Counter[str] = Counter()
        self.error_class = {"local_qwen": None, "codex_cli": None}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("gpt6_luna_high_enabled"), bool):
                self.enabled = data["gpt6_luna_high_enabled"]
        except (OSError, ValueError, AttributeError):
            pass

    def try_admit_request(self) -> bool:
        if self.admitted_requests >= self.max_admitted_requests:
            return False
        self.admitted_requests += 1
        return True

    def release_admitted_request(self) -> None:
        self.admitted_requests -= 1

    async def set_enabled(self, enabled: bool) -> None:
        async with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="providers-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump({"gpt6_luna_high_enabled": enabled}, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.enabled = enabled

    async def record_health(
        self, provider: str, *, ready: bool, latency_ms: int, error_class: str | None
    ) -> None:
        async with self.lock:
            self.last_health_check[provider] = datetime.now(UTC).isoformat()
            self.health_latency_ms[provider] = latency_ms
            self.error_class[provider] = error_class

    async def record_request(self, provider: str, *, latency_ms: int, error_class: str | None) -> None:
        async with self.lock:
            self.counters[provider] += 1
            self.latency_samples[provider] += 1
            self.total_latency_ms[provider] += latency_ms
            self.last_latency_ms[provider] = latency_ms
            if error_class is not None:
                self.failures[provider] += 1
                self.last_failure[provider] = error_class
            self.error_class[provider] = error_class

    async def snapshot(self, provider: str) -> dict[str, Any]:
        async with self.lock:
            return {
                "failures": self.failures[provider],
                "requests": self.counters[provider],
                "last_failure": self.last_failure[provider],
                "last_health_check": self.last_health_check[provider],
                "health_latency_ms": self.health_latency_ms[provider],
                "last_latency_ms": self.last_latency_ms[provider],
                "latency_samples": self.latency_samples[provider],
                "total_latency_ms": self.total_latency_ms[provider],
                "error_class": self.error_class[provider],
            }


class UpstreamResponseTooLarge(ValueError):
    """An upstream exceeded the response byte limit."""


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isdigit():
            raise HTTPException(422, "Invalid request")
        if int(content_length) > MAX_BODY:
            raise HTTPException(413, "Request too large")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(413, "Request too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _request_limited(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None,
    content: bytes | None,
    timeout: float,
    max_response_bytes: int,
) -> httpx.Response:
    async with client.stream(
        method, url, headers=headers, content=content, timeout=timeout
    ) as response:
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_response_bytes:
                raise UpstreamResponseTooLarge()
            chunks.append(chunk)
        return httpx.Response(
            response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/json")},
            content=b"".join(chunks),
            request=response.request,
        )


def create_app(
    *,
    client_token: str | None = None,
    admin_token: str | None = None,
    bridge_token: str | None = None,
    state_path: Path | None = None,
    upstream_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    client_token = client_token if client_token is not None else os.getenv("GATEWAY_CLIENT_TOKEN", "")
    admin_token = admin_token if admin_token is not None else os.getenv("GATEWAY_ADMIN_TOKEN", "")
    bridge_token = bridge_token if bridge_token is not None else os.getenv("BRIDGE_TOKEN", "")
    client_admin_valid = client_admin_tokens_valid(client_token, admin_token)
    luna_credentials_valid = bridge_token_valid(client_token, admin_token, bridge_token)
    state = ProviderState(
        state_path or Path(os.getenv("GATEWAY_STATE_PATH", "/data/providers.json")),
        os.getenv("LUNA_ENABLED", "false").lower() == "true",
    )
    app = FastAPI(title="Local AI Inference Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.providers = state
    app.state.http = upstream_client

    def authorized(value: str | None, expected: str) -> bool:
        return (
            client_admin_valid
            and value is not None
            and hmac.compare_digest(value.encode(), f"Bearer {expected}".encode())
        )

    async def admit_completion(
        authorization: str | None = Header(default=None),
    ):
        if not authorized(authorization, client_token):
            raise HTTPException(401, "Unauthorized")
        if not state.try_admit_request():
            raise HTTPException(503, "Gateway is busy")
        try:
            yield
        finally:
            state.release_admitted_request()

    async def request_upstream(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        max_response_bytes: int = MAX_UPSTREAM_RESPONSE,
    ) -> httpx.Response:
        timeout = 220 if method == "POST" else 2.5
        if app.state.http is None:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
                return await _request_limited(
                    http,
                    method,
                    url,
                    headers=headers,
                    content=content,
                    timeout=timeout,
                    max_response_bytes=max_response_bytes,
                )
        return await _request_limited(
            app.state.http,
            method,
            url,
            headers=headers,
            content=content,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    async def probe(model: str) -> bool:
        provider = "local_qwen" if model == "ddu-narrator" else "codex_cli"
        url = LOCAL_URL if provider == "local_qwen" else LUNA_URL
        token = None if provider == "local_qwen" else bridge_token
        headers = {"Authorization": f"Bearer {token}"} if token else None
        started = perf_counter()
        ready = False
        error_class: str | None = None
        if provider == "codex_cli" and not luna_credentials_valid:
            await state.record_health(
                provider, ready=False, latency_ms=0, error_class="not_configured"
            )
            return False
        try:
            async with state.semaphore:
                response = await request_upstream(
                    "GET", url + "/v1/models", headers=headers, max_response_bytes=MAX_MODEL_RESPONSE
                )
            if response.status_code in {401, 403}:
                error_class = "invalid_credentials"
            elif response.status_code != 200:
                error_class = "upstream_http_error"
            else:
                document = response.json()
                models = document.get("data") if isinstance(document, dict) else None
                if isinstance(models, list):
                    ready = any(
                        isinstance(item, dict) and item.get("id") == model for item in models
                    )
                if not ready:
                    error_class = "model_unavailable"
        except httpx.TimeoutException:
            error_class = "timeout"
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            error_class = "invalid_response"
        latency_ms = max(0, int((perf_counter() - started) * 1000))
        await state.record_health(
            provider, ready=ready, latency_ms=latency_ms, error_class=error_class
        )
        return ready

    @app.get("/health")
    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorized(authorization, client_token):
            raise HTTPException(401, "Unauthorized")
        result = []
        if await probe("ddu-narrator"):
            result.append({"id": "ddu-narrator", "object": "model"})
        if state.enabled and luna_credentials_valid and await probe("gpt-6-luna"):
            result.append({"id": "gpt-6-luna", "object": "model"})
        return {"object": "list", "data": result}

    @app.get("/admin/v1/providers")
    async def provider_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorized(authorization, admin_token):
            raise HTTPException(401, "Unauthorized")
        local_ready, luna_ready = await asyncio.gather(
            probe("ddu-narrator"), probe("gpt-6-luna")
        )
        local_metrics, luna_metrics = await asyncio.gather(
            state.snapshot("local_qwen"), state.snapshot("codex_cli")
        )
        return {
            "providers": [
                {
                    "id": "local_qwen",
                    "model": "ddu-narrator",
                    "configured": True,
                    "enabled": True,
                    "ready": local_ready,
                    **local_metrics,
                },
                {
                    "id": "codex_cli",
                    "model": "gpt-6-luna",
                    "configured": luna_credentials_valid,
                    "enabled": state.enabled,
                    "ready": luna_ready,
                    **luna_metrics,
                },
            ]
        }

    @app.put("/admin/v1/providers/gpt6_luna_high")
    async def toggle(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, bool]:
        if not authorized(authorization, admin_token):
            raise HTTPException(401, "Unauthorized")
        try:
            raw = await _read_limited_body(request)
            body = json.loads(raw)
        except HTTPException:
            raise
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid request") from None
        if not isinstance(body, dict) or set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
            raise HTTPException(422, "Invalid request")
        if body["enabled"] and not luna_credentials_valid:
            raise HTTPException(503, "Provider is not configured")
        await state.set_enabled(body["enabled"])
        return {"enabled": state.enabled}

    @app.post("/v1/chat/completions")
    async def completions(
        request: Request, _admitted: None = Depends(admit_completion)
    ) -> JSONResponse:
        raw = await _read_limited_body(request)
        try:
            document = json.loads(raw)
            if not _is_bounded_json(
                document, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES
            ):
                raise ValueError("request JSON exceeded structural limits")
            body = ChatRequest.model_validate(document)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid request") from None
        if body.stream or body.model not in {"ddu-narrator", "gpt-6-luna"}:
            raise HTTPException(422, "Unsupported model or request option")
        if body.chat_template_kwargs is not None and (
            body.model != "ddu-narrator"
            or set(body.chat_template_kwargs) != {"enable_thinking"}
            or type(body.chat_template_kwargs["enable_thinking"]) is not bool
        ):
            raise HTTPException(422, "Unsupported model or request option")
        provider = "local_qwen" if body.model == "ddu-narrator" else "codex_cli"
        url = LOCAL_URL if provider == "local_qwen" else LUNA_URL
        headers = {"Content-Type": "application/json"}
        if provider == "codex_cli":
            if not luna_credentials_valid:
                raise HTTPException(503, "Provider is not configured")
            if not state.enabled:
                raise HTTPException(503, "Provider is disabled")
            headers["Authorization"] = f"Bearer {bridge_token}"
        if not await probe(body.model):
            if state.error_class[provider] == "timeout":
                raise HTTPException(504, "Provider health check timed out")
            raise HTTPException(503, "Provider is unavailable")
        forward_document = body.model_dump(exclude_none=True)
        forward_body = json.dumps(forward_document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with state.semaphore:
            started = perf_counter()
            error_class: str | None = None
            try:
                response = await request_upstream(
                    "POST", url + "/v1/chat/completions", headers=headers, content=forward_body
                )
                if response.status_code == 401:
                    error_class = "invalid_credentials"
                    raise ValueError()
                if response.status_code == 504:
                    raise httpx.TimeoutException()
                if not 200 <= response.status_code < 300:
                    error_class = "upstream_http_error"
                    raise ValueError()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("model") != body.model:
                    error_class = "model_mismatch"
                    raise ValueError()
                if provider == "codex_cli" and payload.get("provider") != "codex_cli":
                    error_class = "provider_mismatch"
                    raise ValueError()
                if provider == "codex_cli" and payload.get("reasoning_effort") != "high":
                    error_class = "reasoning_effort_mismatch"
                    raise ValueError()
                choices = payload.get("choices")
                if (
                    not isinstance(choices, list)
                    or not choices
                    or not isinstance(choices[0], dict)
                    or not isinstance(choices[0].get("message"), dict)
                    or not isinstance(choices[0]["message"].get("content"), str)
                ):
                    error_class = "malformed_response"
                    raise ValueError()
            except httpx.TimeoutException:
                error_class = "timeout"
                raise HTTPException(504, "Provider timed out") from None
            except UpstreamResponseTooLarge:
                error_class = "response_too_large"
                raise HTTPException(502, "Provider response exceeded the size limit") from None
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                error_class = error_class or "upstream_error"
                raise HTTPException(502, "Provider request failed validation") from None
            finally:
                latency_ms = max(0, int((perf_counter() - started) * 1000))
                await state.record_request(
                    provider, latency_ms=latency_ms, error_class=error_class
                )
        response_headers = {"X-Provider-ID": provider}
        if provider == "codex_cli":
            response_headers["X-Reasoning-Effort"] = "high"
        return JSONResponse(payload, headers=response_headers)

    return app


app = create_app()
