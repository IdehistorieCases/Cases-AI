import json

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

from services.inference_gateway.app import create_app

CLIENT = "client-token-" + "c" * 24
ADMIN = "admin-token-" + "a" * 24
BRIDGE = "bridge-token-" + "b" * 24


def completion_request(model="ddu-narrator"):
    return {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "test_reply",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
        },
    }


def setup(tmp_path, *, luna_enabled=None):
    def upstream(request):
        if request.url.path == "/v1/models":
            model = "gpt-6-luna" if request.url.host == "host.docker.internal" else "ddu-narrator"
            return httpx.Response(200, json={"data": [{"id": model}]})
        return httpx.Response(200, json={
            "model": "gpt-6-luna" if request.url.host == "host.docker.internal" else "ddu-narrator",
            "choices": [{"message": {"content": "{\"ok\":true}"}}],
        })
    transport = httpx.MockTransport(upstream)
    http = httpx.AsyncClient(transport=transport)
    app = create_app(client_token=CLIENT, admin_token=ADMIN, bridge_token=BRIDGE,
                     state_path=tmp_path / "state.json", upstream_client=http)
    if luna_enabled is not None:
        app.state.providers.enabled = luna_enabled
    return app, http


def test_health_is_independent_and_auth_separated(tmp_path):
    app, _ = setup(tmp_path)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models").status_code == 401
    assert client.get("/admin/v1/providers").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {CLIENT}"}).json()["data"] == [
        {"id": "ddu-narrator", "object": "model"}
    ]
    assert client.get("/admin/v1/providers", headers={"Authorization": f"Bearer {CLIENT}"}).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 401


def test_liveness_starts_without_provider_credentials_and_protected_routes_fail_closed(tmp_path):
    app = create_app(client_token="", admin_token="", bridge_token="", state_path=tmp_path / "state.json")
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/models").status_code == 401
    assert client.get("/admin/v1/providers").status_code == 401


def test_reused_tokens_do_not_cross_authentication_boundaries(tmp_path):
    app = create_app(
        client_token=CLIENT,
        admin_token=CLIENT,
        bridge_token=BRIDGE,
        state_path=tmp_path / "state.json",
    )
    client = TestClient(app)
    shared = {"Authorization": f"Bearer {CLIENT}"}
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models", headers=shared).status_code == 401
    assert client.get("/admin/v1/providers", headers=shared).status_code == 401


def test_model_dispatch_and_bridge_auth(tmp_path):
    app, _ = setup(tmp_path, luna_enabled=True)
    client = TestClient(app)
    seen = []
    original_request = app.state.http._transport.handle_async_request

    async def inspect_request(request):
        if request.url.host == "host.docker.internal":
            seen.append(request.headers.get("authorization"))
        return await original_request(request)

    app.state.http._transport.handle_async_request = inspect_request
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {CLIENT}"},
                           json=completion_request("gpt-6-luna"))
    assert response.status_code == 200
    assert response.headers["x-provider-id"] == "codex_cli"
    assert response.headers["x-reasoning-effort"] == "high"
    assert seen == [f"Bearer {BRIDGE}", f"Bearer {BRIDGE}"]


def test_disabled_luna_never_falls_back_and_toggle_persists(tmp_path):
    app, _ = setup(tmp_path)
    client = TestClient(app)
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {CLIENT}"},
                           json=completion_request("gpt-6-luna"))
    assert response.status_code == 503
    enabled = client.put("/admin/v1/providers/gpt6_luna_high", headers={"Authorization": f"Bearer {ADMIN}"},
                         json={"enabled": True})
    assert enabled.status_code == 200
    assert json.loads((tmp_path / "state.json").read_text())["gpt6_luna_high_enabled"] is True
    restarted, _ = setup(tmp_path)
    status = TestClient(restarted).get(
        "/admin/v1/providers", headers={"Authorization": f"Bearer {ADMIN}"}
    )
    assert status.json()["providers"][1]["enabled"] is True


def test_unknown_model_and_mismatched_upstream_rejected(tmp_path):
    app, _ = setup(tmp_path)
    client = TestClient(app)
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {CLIENT}"},
                           json=completion_request("http://bad-host"))
    assert response.status_code == 422


def test_completion_requires_a_bounded_strict_json_schema(tmp_path):
    app, _ = setup(tmp_path)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {CLIENT}"}

    missing = completion_request()
    missing.pop("response_format")
    assert client.post("/v1/chat/completions", headers=headers, json=missing).status_code == 422

    nested_schema = {"type": "string"}
    for _ in range(20):
        nested_schema = {
            "type": "object",
            "properties": {"value": nested_schema},
            "required": ["value"],
        }
    too_deep = completion_request()
    too_deep["response_format"]["json_schema"]["schema"] = nested_schema
    assert client.post("/v1/chat/completions", headers=headers, json=too_deep).status_code == 422

    too_large = completion_request()
    too_large["response_format"]["json_schema"]["schema"]["properties"]["ok"]["description"] = "x" * 25_000
    assert client.post("/v1/chat/completions", headers=headers, json=too_large).status_code == 422


def test_oversized_content_length_is_rejected_before_request_body_buffering(tmp_path, monkeypatch):
    app, _ = setup(tmp_path)
    client = TestClient(app)
    buffered = False
    original_body = Request.body

    async def observe_body(request):
        nonlocal buffered
        buffered = True
        return await original_body(request)

    monkeypatch.setattr(Request, "body", observe_body)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {CLIENT}"},
        content=b"x" * (128_000 + 1),
    )
    assert response.status_code == 413
    assert buffered is False


def test_oversized_upstream_completion_is_rejected(tmp_path):
    def upstream(request):
        model = "gpt-6-luna" if request.url.host == "host.docker.internal" else "ddu-narrator"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model}]})
        return httpx.Response(200, json={"model": model, "choices": [{
            "message": {"content": "x" * 140_000},
        }]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(client_token=CLIENT, admin_token=ADMIN, bridge_token=BRIDGE,
                     state_path=tmp_path / "state.json", upstream_client=http)
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {CLIENT}"},
        json=completion_request(),
    )
    assert response.status_code == 502


def test_upstream_model_mismatch_and_bad_credentials_are_sanitized(tmp_path):
    def upstream(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}]})
        return httpx.Response(401, text=f"{BRIDGE} PRIVATE_PROMPT_SENTINEL")

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(client_token=CLIENT, admin_token=ADMIN, bridge_token=BRIDGE,
                     state_path=tmp_path / "state.json", upstream_client=http)
    app.state.providers.enabled = True
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {CLIENT}"},
        json=completion_request("gpt-6-luna"),
    )
    assert response.status_code == 502
    assert BRIDGE not in response.text
    assert "PRIVATE_PROMPT_SENTINEL" not in response.text
    assert app.state.providers.error_class["codex_cli"] == "invalid_credentials"


def test_malformed_chat_completion_envelope_is_rejected(tmp_path):
    def upstream(request):
        model = "gpt-6-luna" if request.url.host == "host.docker.internal" else "ddu-narrator"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model}]})
        return httpx.Response(200, json={"model": model, "usage": {"total_tokens": 1}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(client_token=CLIENT, admin_token=ADMIN, bridge_token=BRIDGE,
                     state_path=tmp_path / "state.json", upstream_client=http)
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {CLIENT}"},
        json=completion_request(),
    )
    assert response.status_code == 502


def test_timeout_during_provider_probe_is_reported_as_gateway_timeout(tmp_path):
    def upstream(_request):
        raise httpx.ReadTimeout("private upstream detail")

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(client_token=CLIENT, admin_token=ADMIN, bridge_token=BRIDGE,
                     state_path=tmp_path / "state.json", upstream_client=http)
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {CLIENT}"},
        json=completion_request(),
    )
    assert response.status_code == 504
    assert "private upstream detail" not in response.text


def test_admin_status_reports_health_and_latency_counters(tmp_path):
    app, _ = setup(tmp_path)
    client = TestClient(app)
    response = client.get("/admin/v1/providers", headers={"Authorization": f"Bearer {ADMIN}"})
    assert response.status_code == 200
    local = response.json()["providers"][0]
    assert local["last_health_check"]
    assert "error_class" in local
    assert "latency_samples" in local
    assert "total_latency_ms" in local
