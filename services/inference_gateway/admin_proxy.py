from __future__ import annotations

import json
import re
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

UPSTREAM = "http://ddu-gateway:8080"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 32_000
ALLOWED_ROUTES = {
    ("GET", "/admin/v1/providers"),
    ("PUT", "/admin/v1/providers/gpt6_luna_high"),
}
PROVIDER_MODELS = {"local_qwen": "ddu-narrator", "codex_cli": "gpt-6-luna"}
SAFE_ERROR = re.compile(r"^[a-z_]{1,64}$")


async def _read_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None and (
        not content_length.isdigit() or int(content_length) > MAX_REQUEST_BYTES
    ):
        raise ValueError("invalid body size")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise ValueError("invalid body size")
        chunks.append(chunk)
    return b"".join(chunks)


async def _limited_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    authorization: str,
    content: bytes | None,
) -> tuple[int, bytes]:
    async with client.stream(
        method,
        UPSTREAM + path,
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        content=content,
        timeout=4.0,
    ) as response:
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("gateway response exceeded limit")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)


def _safe_status(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, dict) or not isinstance(document.get("providers"), list):
        return None
    if len(document["providers"]) > 2:
        return None
    providers = []
    for provider in document["providers"]:
        if not isinstance(provider, dict):
            return None
        provider_id = provider.get("id")
        if provider_id not in PROVIDER_MODELS or provider.get("model") != PROVIDER_MODELS[provider_id]:
            return None
        boolean_fields = ("configured", "enabled", "ready")
        number_fields = ("failures", "requests", "latency_samples", "total_latency_ms")
        nullable_number_fields = ("health_latency_ms", "last_latency_ms")
        if any(type(provider.get(field)) is not bool for field in boolean_fields):
            return None
        if any(
            isinstance(provider.get(field), bool)
            or not isinstance(provider.get(field), int)
            or provider[field] < 0
            for field in number_fields
        ):
            return None
        if any(
            provider.get(field) is not None
            and (
                isinstance(provider.get(field), bool)
                or not isinstance(provider.get(field), int)
                or provider[field] < 0
            )
            for field in nullable_number_fields
        ):
            return None
        for field in ("last_failure", "error_class"):
            value = provider.get(field)
            if value is not None and (not isinstance(value, str) or not SAFE_ERROR.fullmatch(value)):
                return None
        last_health_check = provider.get("last_health_check")
        if last_health_check is not None and (
            not isinstance(last_health_check, str) or len(last_health_check) > 64
        ):
            return None
        providers.append(
            {
                "id": provider_id,
                "model": PROVIDER_MODELS[provider_id],
                **{field: provider[field] for field in boolean_fields + number_fields + nullable_number_fields},
                "last_failure": provider.get("last_failure"),
                "error_class": provider.get("error_class"),
                "last_health_check": last_health_check,
            }
        )
    return {"providers": providers}


def create_app(*, upstream_client: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.http = upstream_client

    @app.api_route("/{path:path}", methods=["GET", "PUT", "POST", "PATCH", "DELETE"])
    async def forward_admin(request: Request, path: str) -> JSONResponse:
        route = (request.method, "/" + path)
        if route not in ALLOWED_ROUTES or request.url.query:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 2048:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        content = None
        if request.method == "PUT":
            try:
                content = await _read_body(request)
                document = json.loads(content)
            except (ValueError, TypeError, RecursionError):
                return JSONResponse({"detail": "Invalid request"}, status_code=422)
            if (
                not isinstance(document, dict)
                or set(document) != {"enabled"}
                or not isinstance(document["enabled"], bool)
            ):
                return JSONResponse({"detail": "Invalid request"}, status_code=422)
        elif request.headers.get("content-length", "0") not in {"", "0"}:
            return JSONResponse({"detail": "Invalid request"}, status_code=422)

        try:
            if app.state.http is None:
                async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
                    status, raw = await _limited_request(
                        client,
                        request.method,
                        route[1],
                        authorization=authorization,
                        content=content,
                    )
            else:
                status, raw = await _limited_request(
                    app.state.http,
                    request.method,
                    route[1],
                    authorization=authorization,
                    content=content,
                )
        except httpx.TimeoutException:
            return JSONResponse({"detail": "Gateway admin API timed out"}, status_code=504)
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"detail": "Gateway admin API is unavailable"}, status_code=503)

        if status == 401 or status == 403:
            return JSONResponse({"detail": "Gateway admin API unauthorized"}, status_code=401)
        if status != 200:
            return JSONResponse({"detail": "Gateway admin API request failed"}, status_code=502)
        try:
            document = json.loads(raw)
        except (ValueError, TypeError):
            return JSONResponse({"detail": "Gateway admin API response was invalid"}, status_code=502)
        if request.method == "GET":
            safe = _safe_status(document)
            if safe is None:
                return JSONResponse({"detail": "Gateway admin API response was invalid"}, status_code=502)
            return JSONResponse(safe)
        if not isinstance(document, dict) or type(document.get("enabled")) is not bool:
            return JSONResponse({"detail": "Gateway admin API response was invalid"}, status_code=502)
        return JSONResponse({"enabled": document["enabled"]})

    return app


app = create_app()
