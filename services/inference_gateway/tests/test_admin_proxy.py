from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ADMIN = "gateway-admin-token-" + "a" * 24


def load_admin_proxy():
    try:
        return importlib.import_module("services.inference_gateway.admin_proxy")
    except ModuleNotFoundError as error:
        if error.name == "services.inference_gateway.admin_proxy":
            pytest.fail("loopback admin proxy has not been implemented")
        raise


def test_only_admin_proxy_publishes_a_loopback_host_port():
    import yaml

    compose_path = Path(__file__).parents[3] / "deploy" / "inference-gateway" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["inference-gateway"].get("ports") is None
    assert services["admin-proxy"]["ports"] == ["127.0.0.1:18766:8080"]
    assert "gateway_admin_bus" in services["inference-gateway"]["networks"]
    assert services["admin-proxy"]["networks"] == ["gateway_admin_bus"]


def test_admin_proxy_forwards_only_the_two_gateway_admin_routes():
    proxy = load_admin_proxy()
    seen = []

    def upstream(request):
        seen.append(request)
        if request.method == "PUT":
            return httpx.Response(200, json={"enabled": True})
        return httpx.Response(200, json={"providers": []})

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    client = TestClient(proxy.create_app(upstream_client=upstream_client))
    response = client.get(
        "/admin/v1/providers", headers={"Authorization": f"Bearer {ADMIN}"}
    )
    assert response.status_code == 200
    assert response.json() == {"providers": []}
    assert seen[0].url.host == "ddu-gateway"
    assert seen[0].url.path == "/admin/v1/providers"
    assert seen[0].headers["authorization"] == f"Bearer {ADMIN}"

    toggled = client.put(
        "/admin/v1/providers/gpt6_luna_high",
        headers={"Authorization": f"Bearer {ADMIN}"},
        json={"enabled": True},
    )
    assert toggled.status_code == 200
    assert toggled.json() == {"enabled": True}
    assert seen[1].method == "PUT"
    assert seen[1].url.path == "/admin/v1/providers/gpt6_luna_high"
    assert json.loads(seen[1].content) == {"enabled": True}

    for method, path in (
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/admin/v1/providers/gpt6_luna_high"),
        ("PUT", "/admin/v1/providers"),
        ("GET", "/other"),
    ):
        denied = client.request(method, path, headers={"Authorization": f"Bearer {ADMIN}"})
        assert denied.status_code == 404
    assert len(seen) == 2


def test_admin_proxy_redacts_gateway_error_body():
    proxy = load_admin_proxy()

    def upstream(_request):
        return httpx.Response(401, text=ADMIN)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    client = TestClient(proxy.create_app(upstream_client=upstream_client))
    response = client.get(
        "/admin/v1/providers", headers={"Authorization": f"Bearer {ADMIN}"}
    )
    assert response.status_code == 401
    assert ADMIN not in response.text
