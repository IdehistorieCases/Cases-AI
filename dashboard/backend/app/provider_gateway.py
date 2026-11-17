from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.auth import AuthSession, require_csrf, require_session

router = APIRouter(prefix="/api/control/v1/providers", tags=["providers"])
ROOT = Path(__file__).resolve().parents[3]
GATEWAY_URL = "http://127.0.0.1:18766"


class LunaToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _admin_token() -> str | None:
    configured = os.getenv("LOCALAI_GATEWAY_ENV_FILE")
    path = Path(configured).expanduser() if configured else ROOT / "deploy" / "inference-gateway" / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "GATEWAY_ADMIN_TOKEN":
                token = value.strip().strip('"').strip("'")
                return token if len(token) >= 24 else None
    except OSError:
        return None
    return None


async def _gateway(method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _admin_token()
    if token is None:
        raise HTTPException(503, "Inference gateway is not configured")
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.request(
                method, GATEWAY_URL + path, headers={"Authorization": f"Bearer {token}"},
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
    return await _gateway("PUT", "/admin/v1/providers/gpt6_luna_high", body=body.model_dump())
