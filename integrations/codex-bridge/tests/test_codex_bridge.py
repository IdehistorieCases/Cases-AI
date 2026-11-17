import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import BridgeConfig, _matches_schema, create_server, validate_bind_host


@pytest.fixture
def config(tmp_path):
    executable = tmp_path / "fake_codex.py"
    executable.write_text(
        "import json,sys\n"
        "if sys.argv[-2:] == ['login','status']: sys.exit(0)\n"
        "required=['--ignore-user-config','--ignore-rules','--ephemeral','-s','read-only',\n"
        "'--output-schema','-o','features.shell_tool=false','features.multi_agent=false',\n"
        "'features.apps=false','features.remote_plugin=false','features.web_search=false']\n"
        "if any(flag not in sys.argv for flag in required): sys.exit(7)\n"
        "schema=json.load(open(sys.argv[sys.argv.index('--output-schema')+1]))\n"
        "out=sys.argv[sys.argv.index('-o')+1]\n"
        "sys.stdin.read()\n"
        "json.dump({'reply':'hello'},open(out,'w'))\n",
        encoding="utf-8",
    )
    return BridgeConfig(
        bearer_token="a-strong-test-token-1234567890",
        command=(sys.executable, str(executable)),
        timeout_seconds=2,
        max_request_bytes=8192,
        max_schema_bytes=2048,
        max_output_bytes=2048,
    )


@pytest.fixture
def bridge(config):
    server = create_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def request(bridge, path, body=None, token="a-strong-test-token-1234567890"):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(bridge + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_bridge_requires_bearer_and_returns_codex_model(bridge):
    status, _ = request(bridge, "/v1/models", token="wrong")
    assert status == 401
    assert request(bridge, "/health")[1] == {"status": "ok"}
    assert request(bridge, "/v1/models")[1]["data"][0]["id"] == "gpt-6-luna"


def test_bridge_bind_address_must_be_loopback_or_private():
    validate_bind_host("127.0.0.1")
    validate_bind_host("192.168.65.254")
    with pytest.raises(ValueError, match="private IP"):
        validate_bind_host("0.0.0.0")
    with pytest.raises(ValueError, match="private IP"):
        validate_bind_host("8.8.8.8")


def test_stalled_http_clients_release_bridge_slots(config):
    config = replace(config, max_concurrency=2, request_read_deadline_seconds=0.15)
    server = create_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stalled = []
    try:
        address = ("127.0.0.1", server.server_port)
        for _ in range(config.max_concurrency * 2):
            connection = socket.create_connection(address, timeout=2)
            connection.sendall(b"GET /health HTTP/1.1\r\nHost: ")
            stalled.append(connection)
        time.sleep(0.4)
        assert request(f"http://127.0.0.1:{server.server_port}", "/health")[1] == {"status": "ok"}
        for connection in stalled:
            connection.close()
        stalled.clear()

        body_headers = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: 100\r\n"
            b"Authorization: Bearer a-strong-test-token-1234567890\r\n\r\n{"
        )
        for _ in range(config.max_concurrency * 2):
            connection = socket.create_connection(address, timeout=2)
            connection.sendall(body_headers)
            stalled.append(connection)
        time.sleep(0.4)
        assert request(f"http://127.0.0.1:{server.server_port}", "/health")[1] == {"status": "ok"}
    finally:
        for connection in stalled:
            connection.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_trickling_http_clients_hit_total_request_deadline(config):
    config = replace(config, max_concurrency=2, request_read_deadline_seconds=1.0)
    server = create_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stalled = []
    try:
        address = ("127.0.0.1", server.server_port)
        for _ in range(config.max_concurrency * 2):
            connection = socket.create_connection(address, timeout=2)
            connection.sendall(b"G")
            stalled.append(connection)
        for byte in (b"E", b"T", b" ", b"/health", b" HTTP/1.1"):
            time.sleep(0.15)
            for connection in stalled:
                connection.sendall(byte)
        time.sleep(0.35)  # Last-byte idle time is below 1s; only the total deadline frees slots.
        assert request(f"http://127.0.0.1:{server.server_port}", "/health")[1] == {"status": "ok"}
        for connection in stalled:
            connection.close()
        stalled.clear()

        body_headers = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Length: 100\r\n"
            b"Authorization: Bearer a-strong-test-token-1234567890\r\n\r\n{"
        )
        for _ in range(config.max_concurrency * 2):
            connection = socket.create_connection(address, timeout=2)
            connection.sendall(body_headers)
            stalled.append(connection)
        for _ in range(6):
            time.sleep(0.12)
            for connection in stalled:
                connection.sendall(b"x")
        time.sleep(0.35)
        assert request(f"http://127.0.0.1:{server.server_port}", "/health")[1] == {"status": "ok"}
    finally:
        for connection in stalled:
            connection.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_bridge_reports_missing_codex_login_as_unavailable(config, tmp_path):
    no_login = tmp_path / "no_login.py"
    no_login.write_text("import sys; sys.exit(2)", encoding="utf-8")
    unauthenticated = BridgeConfig(
        bearer_token=config.bearer_token,
        command=(sys.executable, str(no_login)),
    )
    server = create_server(unauthenticated, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = request(f"http://127.0.0.1:{server.server_port}", "/v1/models")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert status == 503


def test_bridge_returns_schema_constrained_completion(bridge):
    payload = {
        "model": "gpt-6-luna",
        "messages": [
            {"role": "system", "content": "Follow schema"},
            {"role": "user", "content": "Do this"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "reply",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"reply": {"type": "string"}},
                    "required": ["reply"],
                    "additionalProperties": False,
                },
            },
        },
    }
    status, document = request(bridge, "/v1/chat/completions", payload)
    assert status == 200
    assert document["model"] == "gpt-6-luna"
    assert json.loads(document["choices"][0]["message"]["content"]) == {"reply": "hello"}
    assert document["provider"] == "codex_cli"
    assert document["reasoning_effort"] == "high"


def test_bridge_rejects_wrong_model(bridge):
    status, _ = request(bridge, "/v1/chat/completions", {"model": "other"})
    assert status == 400


def test_bridge_rejects_oversized_body(bridge):
    status, _ = request(bridge, "/v1/chat/completions", {"padding": "x" * 9000})
    assert status == 413


def test_bridge_times_out_without_falling_back(config, tmp_path):
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(2)", encoding="utf-8")
    slow_config = BridgeConfig(
        bearer_token=config.bearer_token,
        command=(sys.executable, str(slow)),
        timeout_seconds=0.1,
    )
    server = create_server(slow_config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "hello"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "reply",
                "strict": True,
                "schema": {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]},
            },
        },
    }
    try:
        status, _ = request(f"http://127.0.0.1:{server.server_port}", "/v1/chat/completions", payload)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert status == 504


def test_bridge_rejects_json_that_violates_schema(config, tmp_path):
    invalid = tmp_path / "invalid.py"
    invalid.write_text(
        "import json,sys\n"
        "if sys.argv[-2:] == ['login','status']: sys.exit(0)\n"
        "out=sys.argv[sys.argv.index('-o')+1]\n"
        "json.dump({'reply':9},open(out,'w'))\n",
        encoding="utf-8",
    )
    invalid_config = BridgeConfig(
        bearer_token=config.bearer_token,
        command=(sys.executable, str(invalid)),
    )
    server = create_server(invalid_config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "hello"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "reply",
                "strict": True,
                "schema": {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]},
            },
        },
    }
    try:
        status, _ = request(f"http://127.0.0.1:{server.server_port}", "/v1/chat/completions", payload)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert status == 502


def test_bridge_rejects_malformed_json_output(config, tmp_path):
    invalid = tmp_path / "malformed.py"
    invalid.write_text(
        "import sys\n"
        "if sys.argv[-2:] == ['login','status']: sys.exit(0)\n"
        "out=sys.argv[sys.argv.index('-o')+1]\n"
        "open(out,'w').write('{not json')\n",
        encoding="utf-8",
    )
    invalid_config = BridgeConfig(
        bearer_token=config.bearer_token,
        command=(sys.executable, str(invalid)),
    )
    server = create_server(invalid_config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "hello"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "reply",
                "strict": True,
                "schema": {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]},
            },
        },
    }
    try:
        status, _ = request(f"http://127.0.0.1:{server.server_port}", "/v1/chat/completions", payload)
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert status == 502


def test_bridge_validates_nested_references_and_nullable_schema():
    schema = {
        "type": "object",
        "properties": {
            "reply": {"$ref": "#/$defs/reply"},
            "optional": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
            },
        },
        "required": ["reply", "optional"],
        "additionalProperties": False,
        "$defs": {
            "reply": {
                "type": "string",
                "minLength": 1,
                "pattern": "\\S",
            }
        },
    }

    assert _matches_schema({"reply": "hello", "optional": None}, schema)
    assert not _matches_schema({"reply": " ", "optional": "ok"}, schema)
