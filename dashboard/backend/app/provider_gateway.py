from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from app.auth import AuthSession, require_csrf, require_session

router = APIRouter(prefix="/api/control/v1/providers", tags=["providers"])
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GATEWAY_PORT = 18766


class LunaToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    cloud_consent: StrictBool = False


def _gateway_env_file() -> Path:
    configured = os.getenv("LOCALAI_GATEWAY_ENV_FILE")
    return (
        Path(configured).expanduser()
        if configured
        else ROOT / "deploy" / "inference-gateway" / ".env"
    )


def _gateway_setting(name: str) -> str | None:
    try:
        for line in _gateway_env_file().read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _admin_token() -> str | None:
    token = _gateway_setting("GATEWAY_ADMIN_TOKEN")
    return token if token and len(token) >= 24 else None


def _gateway_url() -> str:
    raw_port = _gateway_setting("GATEWAY_ADMIN_HOST_PORT")
    try:
        port = int(raw_port) if raw_port else DEFAULT_GATEWAY_PORT
    except ValueError:
        port = DEFAULT_GATEWAY_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_GATEWAY_PORT
    return f"http://127.0.0.1:{port}"


async def _gateway(method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _admin_token()
    if token is None:
        raise HTTPException(503, "Inference gateway is not configured")
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.request(
                method, _gateway_url() + path, headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
    except httpx.HTTPError:
        raise HTTPException(503, "Inference gateway is unavailable") from None
    if response.status_code == 401:
        raise HTTPException(503, "Inference gateway rejected its configured admin token")
    if not 200 <= response.status_code < 300:
        raise HTTPException(502, "Inference gateway request failed")
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(502, "Inference gateway returned an invalid response") from None
    return result if isinstance(result, dict) else {}


@router.get("")
async def provider_status(
    _session: Annotated[AuthSession, Depends(require_session)],
) -> dict[str, Any]:
    return await _gateway("GET", "/admin/v1/providers")


@router.put("/gpt6_luna_high")
async def set_luna(
    body: LunaToggle,
    _session: Annotated[AuthSession, Depends(require_csrf)],
) -> dict[str, Any]:
    if body.enabled and not body.cloud_consent:
        raise HTTPException(422, "Explicit cloud data consent is required to enable GPT-6 Luna")
    if body.enabled:
        status = await _gateway("GET", "/admin/v1/providers")
        providers = status.get("providers")
        luna = next(
            (item for item in providers if isinstance(item, dict) and item.get("id") == "codex_cli"),
            None,
        ) if isinstance(providers, list) else None
        if not isinstance(luna, dict) or luna.get("configured") is not True or luna.get("ready") is not True:
            raise HTTPException(409, "GPT-6 Luna is not configured and ready")
    return await _gateway(
        "PUT", "/admin/v1/providers/gpt6_luna_high", body={"enabled": body.enabled}
    )
