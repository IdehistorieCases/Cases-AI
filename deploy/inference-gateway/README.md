# Inference gateway runbook

The inference-gateway joins the external `ddu_inference_bus` and has no host port. In the default deployment, clients reach it as `ddu-gateway:8080`. The admin proxy publishes only provider status and Luna toggle routes on loopback; inference endpoints remain private to the Docker network. The gateway state volume stores the Luna enabled flag, and a fresh install defaults to disabled.

Compose supports environment overrides for the gateway and admin-proxy container names, the gateway network alias, the admin host port, and the proxy's upstream URL. These are used by [Cases-Startup](https://github.com/IdehistorieCases/Cases-Startup) to give its gateway a distinct identity, port, and project-scoped state while keeping the existing game stack untouched. Its backend uses the distinct alias `cases-startup-gateway:8080`.

The dashboard reads `GATEWAY_ADMIN_TOKEN` and `GATEWAY_ADMIN_HOST_PORT` from this directory's private `.env`; the frontend never receives the token. `BRIDGE_TOKEN` is passed only to the gateway container. All three credentials must be independent random values of at least 24 characters.

## Configure and start

1. Copy `.env.example` to `.env`. Replace every placeholder with a different random token. Keep `.env` untracked and do not paste values into chat, terminals that record transcripts, or logs.
2. Ensure the Docker network `ddu_inference_bus` exists. Local Qwen is optional for a Luna-only deployment; start it only when a session explicitly selects `local_qwen`.
3. Start the gateway with Luna disabled:

    ```powershell
    docker compose --env-file deploy/inference-gateway/.env -f deploy/inference-gateway/compose.yaml up -d --build
    ```

4. Start the Codex bridge on loopback using the configured `LUNA_BRIDGE_PORT`. If another bridge owns the default port, use a free loopback port and set `LUNA_BRIDGE_PORT` to match.
5. Check `/health`, authenticated `/v1/models`, and dashboard Providers. For Luna sessions, confirm the bridge is ready before enabling Luna. Do not send a completion until the cloud-data disclosure is acknowledged in the dashboard.

Set each client on `ddu_inference_bus` to the gateway's configured network alias and `GATEWAY_CLIENT_TOKEN`. Never give clients the admin or bridge token. Dashboard controls require the normal administrator login and CSRF session.

## Security and rollback

The health response is a minimal liveness status. The gateway process refuses placeholder or reused client/admin credentials; a missing bridge token disables only Luna while local Qwen remains usable. Model discovery and completion require the client token, and admin routes require a different token. Provider URLs are fixed in code; Luna never falls back to local Qwen. The gateway bounds request and upstream response bodies, JSON/schema depth, concurrency, and provider timeouts. It logs no prompt content and runs with a read-only container root. Only the allowlisted admin proxy binds to loopback.

To disable Luna, use the dashboard Providers page or the authenticated admin API through loopback. To roll back, stop only the Compose project used for this deployment:

    ```powershell
    docker compose --env-file deploy/inference-gateway/.env -f deploy/inference-gateway/compose.yaml down
    ```

The named state volume is preserved unless explicitly removed.

For the coordinated CASES game startup and the ChatGPT 6 Luna session default, use [IdehistorieCases/Cases-Startup](https://github.com/IdehistorieCases/Cases-Startup).
