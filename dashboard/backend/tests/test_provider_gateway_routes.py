from conftest import AsgiTestClient, load_dashboard_module


def client(tmp_path):
    api = load_dashboard_module("api")
    app = api.create_app(metadata_path=tmp_path / "metadata.sqlite3", frontend_dist=tmp_path / "frontend",
                         operation_coordinator=object())
    return AsgiTestClient(app)


def test_provider_routes_require_session_and_csrf(tmp_path, monkeypatch):
    import app.provider_gateway as proxy

    calls = []
    async def fake_gateway(method, path, *, body=None):
        calls.append((method, path, body))
        return {"providers": []} if method == "GET" else {"enabled": body["enabled"]}
    monkeypatch.setattr(proxy, "_gateway", fake_gateway)
    c = client(tmp_path)
    assert c.get("/api/control/v1/providers").status_code == 401
    c.setup_admin()
    assert c.get("/api/control/v1/providers").status_code == 200
    denied = c.request("PUT", "/api/control/v1/providers/gpt6_luna_high",
                       json_body={"enabled": True})
    assert denied.status_code == 403
    allowed = c.request("PUT", "/api/control/v1/providers/gpt6_luna_high",
                        json_body={"enabled": True}, headers={"X-CSRF-Token": c.csrf_token or ""})
    assert allowed.status_code == 200
    assert calls == [("GET", "/admin/v1/providers", None),
                     ("PUT", "/admin/v1/providers/gpt6_luna_high", {"enabled": True})]


def test_provider_toggle_rejects_extra_fields(tmp_path):
    c = client(tmp_path)
    c.setup_admin()
    response = c.request("PUT", "/api/control/v1/providers/gpt6_luna_high",
                         json_body={"enabled": True, "token": "leak"},
                         headers={"X-CSRF-Token": c.csrf_token or ""})
    assert response.status_code == 422


def test_gateway_admin_token_stays_server_side_and_gateway_auth_failure_is_sanitized(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import httpx

    import app.provider_gateway as proxy

    token = "gateway-admin-secret-" + "s" * 24
    env_file = tmp_path / "gateway.env"
    env_file.write_text(f"GATEWAY_ADMIN_TOKEN={token}\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAI_GATEWAY_ENV_FILE", str(env_file))
    gateway_status = {"code": 200}
    sent_headers = []
    c = client(tmp_path)
    c.setup_admin()

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, _method, _url, *, headers, json=None):
            sent_headers.append(headers)
            if gateway_status["code"] == 401:
                return httpx.Response(401, text=token)
            return httpx.Response(200, json={"providers": []})

    monkeypatch.setattr(
        proxy, "httpx", SimpleNamespace(AsyncClient=FakeClient, HTTPError=httpx.HTTPError)
    )
    ok = c.get("/api/control/v1/providers")
    assert ok.status_code == 200
    assert token not in ok.text
    assert sent_headers[-1]["Authorization"] == f"Bearer {token}"

    gateway_status["code"] = 401
    denied = c.get("/api/control/v1/providers")
    assert denied.status_code == 503
    assert token not in denied.text
