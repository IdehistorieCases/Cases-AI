# Inference gateway runbook

The inference-gateway joins the existing external ddu_inference_bus and has no host port. CASES and other clients reach it as `ddu-gateway:8080`. A separate admin-proxy on an isolated Docker network publishes `127.0.0.1:18766`; it forwards only provider status and Luna toggle routes, so inference endpoints stay private to ddu_inference_bus. The gateway state volume stores the Luna enabled flag; a fresh install defaults to disabled. The dashboard reads GATEWAY_ADMIN_TOKEN from this directory's private .env; the frontend never receives the token. BRIDGE_TOKEN is passed only to the gateway container. All three values must be independent random values of at least 24 characters.

## Configure and start

1. Copy .env.example to .env. Replace every placeholder with a different random token using a password manager or a local secure random generator. Keep .env untracked and do not paste values into chat, terminals that record transcripts, or logs.
2. Ensure the Docker network ddu_inference_bus already exists and that local Qwen is reachable as ddu-llm:8080.
3. Start the gateway from the repository root with Luna still disabled:

    docker compose --env-file deploy/inference-gateway/.env -f deploy/inference-gateway/compose.yaml up -d --build

4. Follow the bridge README to resolve `host.docker.internal` from `ddu-gateway`, verify that exact address is assigned to a private Docker Desktop/WSL adapter on Windows, set `LOCALAI_CODEX_BRIDGE_HOST`, and start the bridge. The bridge's default loopback bind is for local-only use; it cannot receive connections addressed to the host's separate Docker interface IP. Keep Windows Firewall enabled and scope access to the Docker private adapter/profile.
5. Query `/health`, authenticated `/v1/models`, and dashboard Providers. Confirm Qwen and the bridge are ready before enabling Luna. Do not send a completion until the cloud disclosure is acknowledged in the dashboard.

Set CASES or another container on `ddu_inference_bus` to `http://ddu-gateway:8080` and the `GATEWAY_CLIENT_TOKEN`; never give it either admin or bridge token. The loopback host port `http://127.0.0.1:18766` serves only the admin proxy. Dashboard controls require the normal administrator login and CSRF session.

## Security and rollback

The health response is a minimal liveness status. The gateway process refuses placeholder or reused client/admin credentials; a missing bridge token disables only Luna while local Qwen remains usable. Model discovery and completion require the client token, and admin routes require a different token. Provider URLs are fixed in code; Luna never falls back to local Qwen. The gateway bounds request and upstream response bodies, JSON/schema depth, concurrent admitted requests, upstream concurrency, and provider timeouts. It logs no prompt content and runs with a read-only container root. The gateway publishes no host port; only the allowlisted admin proxy binds to loopback. Dashboard status shows configured, enabled, ready, last health check, safe error class, and latency counters.

To disable Luna, use the dashboard Providers page or the authenticated admin API through loopback. To roll back the gateway, stop only this Compose project:

    docker compose --env-file deploy/inference-gateway/.env -f deploy/inference-gateway/compose.yaml down

Then stop the Windows bridge. Existing root Compose vLLM controls are independent. Removing the named gateway-state volume resets provider enablement to the safe disabled default.
