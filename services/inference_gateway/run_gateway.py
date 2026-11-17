"""Validate required credentials before starting the gateway HTTP server."""

from __future__ import annotations

import logging
import os

import uvicorn
from app import app, bridge_token_valid, client_admin_tokens_valid

logger = logging.getLogger(__name__)

client_token = os.getenv("GATEWAY_CLIENT_TOKEN", "")
admin_token = os.getenv("GATEWAY_ADMIN_TOKEN", "")
bridge_token = os.getenv("BRIDGE_TOKEN", "")

if not client_admin_tokens_valid(client_token, admin_token):
    raise SystemExit("Set distinct non-placeholder GATEWAY_CLIENT_TOKEN and GATEWAY_ADMIN_TOKEN values")
if not bridge_token_valid(client_token, admin_token, bridge_token):
    logger.warning("Codex bridge token is missing, duplicated, or a placeholder; Luna will remain unavailable")

uvicorn.run(app, host="0.0.0.0", port=8080, limit_concurrency=16)
