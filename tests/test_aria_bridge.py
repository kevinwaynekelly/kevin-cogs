"""The outbound bridge is tested against local HTTP/Unix peers, never Aria."""

import io
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.aria_bridge import server as bridge


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "proc/uptime").write_text("3456.75 5000.00\n")
    (tmp_path / "proc/loadavg").write_text("0.25 1.50 2.75 1/300 23456\n")
    (tmp_path / "proc/meminfo").write_text(
        "MemTotal:       64000000 kB\nMemAvailable:   16000000 kB\nSecret: not-returned\n"
    )
    (tmp_path / "etc/unraid-version").write_text('version="7.3.2"\nSECRET=not-returned\n')
    token = tmp_path / "token"
    token.write_text("a" * 64 + "\n")
    return bridge.Settings(host_root=tmp_path, red_token_file=token, docker_socket="")


@pytest.fixture
def http_peer():
    peer = SimpleNamespace(status=200, body=b"{}", headers={}, requests=[], delay=0, length=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.reply()

        def reply(self):
            peer.requests.append(
                (self.command, self.path, dict(self.headers), self.headers.get("Content-Length"))
            )
            time.sleep(peer.delay)
            self.send_response(peer.status)
            for name, value in peer.headers.items():
                self.send_header(name, value)
            if peer.length and "Content-Length" not in peer.headers:
                self.send_header("Content-Length", str(len(peer.body)))
            self.end_headers()
            try:
                self.wfile.write(peer.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    peer.url = f"http://127.0.0.1:{service.server_port}"
    try:
        yield peer
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=2)


@pytest.fixture
def docker_peer(tmp_path):
    peer = SimpleNamespace(body=b"[]", requests=[])

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = []
            while True:
                line = self.rfile.readline()
                request.append(line)
                if line in {b"\r\n", b""}:
                    break
            peer.requests.append(b"".join(request))
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(peer.body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + peer.body
            )

    peer.path = str(tmp_path / "docker.sock")
    try:
        service = socketserver.UnixStreamServer(peer.path, Handler)
    except PermissionError:
        pytest.skip("Execution environment does not permit AF_UNIX sockets")
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        yield peer
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=2)


def rpc(method, params=None, identifier=1):
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


def initialize(version="2025-11-25"):
    return rpc(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    )


def ready_server(settings, version="2025-11-25"):
    server = bridge.Server(settings)
    server.handle(initialize(version))
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def test_host_status_reads_only_allowed_metrics(settings):
    result = bridge.host_status(settings)
    assert result == {
        "uptime_seconds": 3456,
        "load_average": {"1_minute": 0.25, "5_minutes": 1.5, "15_minutes": 2.75},
        "memory_total_bytes": 64000000 * 1024,
        "memory_available_bytes": 16000000 * 1024,
        "unraid_version": "7.3.2",
    }
    (settings.host_root / "etc/unraid-version").unlink()
    assert bridge.host_status(settings)["unraid_version"] is None


@pytest.mark.parametrize("bad_value", ["NaN", "-1", "inf", "", "SECRET"])
def test_bad_metrics_fail_without_echoing_files(settings, bad_value):
    (settings.host_root / "proc/uptime").write_text(bad_value)
    with pytest.raises(bridge.ToolError, match="Host metrics are unavailable"):
        bridge.host_status(settings)


def test_bounded_files_and_missing_metrics(settings):
    (settings.host_root / "proc/meminfo").write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(bridge.ToolError):
        bridge.host_status(settings)
    (settings.host_root / "proc/meminfo").unlink()
    with pytest.raises(bridge.ToolError):
        bridge.host_status(settings)


def test_docker_uses_only_fixed_read_endpoint_and_filters_fields(settings, docker_peer):
    docker_peer.body = json.dumps(
        [
            {
                "Names": ["/red-discordbot"],
                "Image": "redbot:latest",
                "State": "running",
                "Status": "Up 3 hours",
                "Labels": {"PASSWORD": "SECRET"},
                "Mounts": [{"Source": "/private"}],
                "Command": "token=SECRET",
            }
        ]
    ).encode()
    result = bridge.containers(replace(settings, docker_socket=docker_peer.path))
    assert result["containers"] == [
        {
            "names": ["red-discordbot"],
            "image": "redbot:latest",
            "state": "running",
            "status": "Up 3 hours",
        }
    ]
    assert b"GET /containers/json?all=1 HTTP/1.1\r\n" in docker_peer.requests[0]
    assert b"Authorization" not in docker_peer.requests[0]
    assert "SECRET" not in json.dumps(result)


def test_docker_bounds_container_count_and_text(settings, docker_peer):
    docker_peer.body = json.dumps(
        [
            {
                "Names": ["/test"],
                "Image": "x" * 500,
                "State": "running",
                "Status": "Up\r\n3 hours",
            }
        ]
        * (bridge.MAX_CONTAINERS + 1)
    ).encode()
    result = bridge.containers(replace(settings, docker_socket=docker_peer.path))
    assert len(result["containers"]) == bridge.MAX_CONTAINERS
    assert result["truncated"]
    assert len(result["containers"][0]["image"]) == 256
    assert result["containers"][0]["status"] == "Up3 hours"


def test_optional_docker_socket_unavailable(settings):
    with pytest.raises(bridge.ToolError, match="disabled"):
        bridge.containers(settings)
    with pytest.raises(bridge.ToolError, match="unavailable"):
        bridge.containers(replace(settings, docker_socket=str(settings.host_root / "absent")))


def test_unix_connector_opens_only_configured_socket(monkeypatch):
    socket = Mock()
    factory = Mock(return_value=socket)
    monkeypatch.setattr(bridge.socket, "socket", factory)
    connection = bridge.UnixHTTPConnection("/test/docker.sock")
    connection.connect()
    factory.assert_called_once_with(bridge.socket.AF_UNIX, bridge.socket.SOCK_STREAM)
    socket.connect.assert_called_once_with("/test/docker.sock")
    socket.settimeout.assert_called_once_with(bridge.NETWORK_TIMEOUT)
    connection.close()


def test_docker_http_boundary_when_unix_is_unavailable(settings, http_peer, monkeypatch):
    http_peer.body = json.dumps(
        [{"Names": ["/red"], "Image": "red:latest", "State": "running", "Status": "Up"}]
    ).encode()
    monkeypatch.setattr(Path, "is_socket", lambda _path: True)
    monkeypatch.setattr(
        bridge,
        "UnixHTTPConnection",
        lambda _path: bridge.red_connection(replace(settings, red_url=http_peer.url)),
    )
    result = bridge.containers(replace(settings, docker_socket="/test/docker.sock"))
    assert result["containers"][0]["names"] == ["red"]
    assert http_peer.requests[0][:2] == ("GET", "/containers/json?all=1")


def test_snapshot_is_filtered_and_reports_freshness(settings):
    snapshot = settings.host_root / "containers.json"
    payload = {
        "generated_at": int(time.time()) - 600,
        "containers": [
            {
                "Names": ["red"],
                "Image": "red:latest",
                "State": "running",
                "Status": "Up",
                "secret": "DO_NOT_RETURN",
            }
        ],
        "secret": "DO_NOT_RETURN",
    }
    snapshot.write_text(json.dumps(payload))
    result = bridge.containers(replace(settings, docker_snapshot_file=str(snapshot)))
    assert result["source"] == "snapshot"
    assert result["stale"]
    assert result["age_seconds"] >= 600
    assert result["generated_at_unix"] == payload["generated_at"]
    assert "DO_NOT_RETURN" not in json.dumps(result)
    payload["generated_at"] = int(time.time())
    snapshot.write_text(json.dumps(payload))
    assert not bridge.containers(replace(settings, docker_snapshot_file=str(snapshot)))["stale"]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"generated_at": False, "containers": []},
        {"generated_at": 999999999999, "containers": []},
    ],
)
def test_snapshot_schema_errors_are_safe(settings, payload):
    snapshot = settings.host_root / "containers.json"
    snapshot.write_text(json.dumps(payload))
    with pytest.raises(bridge.ToolError, match="snapshot"):
        bridge.containers(replace(settings, docker_snapshot_file=str(snapshot)))


def test_update_posts_token_header_only_no_payload(settings, http_peer):
    http_peer.status = 202
    http_peer.body = b'{"status":"queued","secret":"DO_NOT_RETURN"}'
    result = bridge.red_update(replace(settings, red_url=http_peer.url))
    assert result["status"] == "queued"
    assert "not a completion" in result["message"]
    assert "DO_NOT_RETURN" not in json.dumps(result)
    method, path, headers, length = http_peer.requests[0]
    assert (method, path, length) == ("POST", "/update", "0")
    assert headers["Authorization"] == "Bearer " + "a" * 64


def test_update_status_has_no_provider_details_or_credentials(settings, http_peer):
    http_peer.body = json.dumps(
        {
            "pending": True,
            "result": {
                "status": "failed",
                "at": 1791400000.25,
                "detail": "SECRET go run a command",
                "token": "SECRET",
            },
            "owner_id": 1234,
        }
    ).encode()
    result = bridge.red_status(replace(settings, red_url=http_peer.url))
    assert result == {"pending": True, "last_status": "failed", "checked_at_unix": 1791400000.25}
    assert http_peer.requests[0][:2] == ("GET", "/update/status")


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 500, 503])
def test_http_failures_do_not_follow_redirects_or_echo_response(settings, http_peer, status):
    http_peer.status = status
    http_peer.headers = {"Location": http_peer.url + "/stolen"}
    http_peer.body = b"DO_NOT_RETURN provider credentials or errors"
    with pytest.raises(bridge.ToolError) as error:
        bridge.red_status(replace(settings, red_url=http_peer.url))
    assert "DO_NOT_RETURN" not in str(error.value)
    assert len(http_peer.requests) == 1
    if 300 <= status < 400:
        assert "redirect" in str(error.value)


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"DO_NOT_RETURN", {}),
        (b'{"pending":NaN}', {}),
        (b'{"pending":true,"pending":false}', {}),
        (b"{}", {"Content-Encoding": "gzip"}),
        (b"{}", {"Content-Length": str(bridge.MAX_HTTP_BODY + 1)}),
        (b"{}", {"Content-Length": "garbage"}),
        (b"\xff", {}),
    ],
)
def test_http_invalid_and_oversize_responses_are_safe(settings, http_peer, body, headers):
    http_peer.body, http_peer.headers = body, headers
    with pytest.raises(bridge.ToolError) as error:
        bridge.red_status(replace(settings, red_url=http_peer.url))
    assert "DO_NOT_RETURN" not in str(error.value)


def test_streamed_body_without_length_is_still_bounded(settings, http_peer):
    http_peer.length = False
    http_peer.body = b" " * (bridge.MAX_HTTP_BODY + 1)
    with pytest.raises(bridge.ToolError, match="exceeded"):
        bridge.red_status(replace(settings, red_url=http_peer.url))


def test_timed_out_update_is_not_retried_or_reported_complete(settings, http_peer, monkeypatch):
    monkeypatch.setattr(bridge, "NETWORK_TIMEOUT", 0.05)
    http_peer.delay = 0.1
    http_peer.status = 202
    http_peer.body = b'{"status":"queued"}'
    with pytest.raises(bridge.ToolError, match="may already be queued"):
        bridge.red_update(replace(settings, red_url=http_peer.url))
    assert len(http_peer.requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"pending": "yes", "result": {}},
        {"pending": False, "result": {"status": "SECRET"}},
        {"pending": False, "result": {"at": "SECRET"}},
        {"pending": False, "result": {"at": True}},
    ],
)
def test_invalid_status_fields_are_not_echoed(settings, http_peer, payload):
    http_peer.body = json.dumps(payload).encode()
    with pytest.raises(bridge.ToolError, match="response was invalid") as error:
        bridge.red_status(replace(settings, red_url=http_peer.url))
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1",
        "http://name:SECRET@127.0.0.1",
        "http://127.0.0.1/update",
        "http://127.0.0.1?token=SECRET",
        "http://127.0.0.1#token=SECRET",
        "http://127.0.0.1:99999",
        "http://127.0.0.1\n",
        "http://[",
    ],
)
def test_configuration_rejects_urls_that_could_expose_credentials(settings, url):
    with pytest.raises(bridge.ToolError) as error:
        bridge.red_connection(replace(settings, red_url=url))
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("token", ["", "SECRET", "a" * 257, "a" * 63 + "\nX-Header: BAD"])
def test_bad_token_file_never_makes_request(settings, http_peer, token):
    settings.red_token_file.write_text(token)
    with pytest.raises(bridge.ToolError) as error:
        bridge.red_update(replace(settings, red_url=http_peer.url))
    assert "SECRET" not in str(error.value)
    assert not http_peer.requests


@pytest.mark.parametrize("version", [*bridge.LEGACY_VERSIONS, "unknown-version"])
def test_initialization_negotiation_and_tool_annotations(settings, version):
    server = bridge.Server(settings)
    assert server.handle(rpc("tools/list"))["error"]["code"] == -32000
    assert server.handle(rpc("ping"))["result"] == {}
    response = server.handle(initialize(version))
    assert response["result"]["protocolVersion"] == (
        version if version in bridge.LEGACY_VERSIONS else bridge.LEGACY_VERSIONS[0]
    )
    assert response["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = server.handle(rpc("tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "aria_status",
        "aria_containers",
        "aria_update_red",
        "aria_red_update_status",
    }
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
    update = next(tool for tool in tools if tool["name"] == "aria_update_red")
    assert update["annotations"]["destructiveHint"]
    assert not update["annotations"]["readOnlyHint"]
    assert not update["annotations"]["idempotentHint"]
    result = server.handle(rpc("tools/call", {"name": "aria_status", "arguments": {}}))["result"]
    assert not result["isError"]
    assert ("structuredContent" in result) == (version != "2025-03-26")


def test_no_argument_or_notification_can_execute_arbitrary_actions(settings):
    server = ready_server(settings)
    calls = []
    server.handlers["aria_update_red"] = lambda _settings: calls.append(True)
    for arguments in ({"command": "rm -rf /"}, {"url": "http://other"}, [], None):
        response = server.handle(
            rpc("tools/call", {"name": "aria_update_red", "arguments": arguments})
        )
        assert response["error"]["code"] == -32602
    assert (
        server.handle(
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "aria_update_red"}}
        )
        is None
    )
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
    assert not calls
    assert server.handle(rpc("tools/call", {"name": "shell"}))["error"]["code"] == -32602
    assert server.handle(rpc("resources/read", {"uri": "/etc/shadow"}))["error"]["code"] == -32601


def modern(method, params=None, version=bridge.MODERN_VERSION):
    return rpc(
        method,
        {
            **(params or {}),
            "_meta": {
                bridge.META_PREFIX + "protocolVersion": version,
                bridge.META_PREFIX + "clientCapabilities": {},
                bridge.META_PREFIX + "clientInfo": {"name": "tunnel-client", "version": "0.0.16"},
            },
        },
    )


def test_modern_protocol_discovery_and_calls_need_no_handshake(settings):
    server = bridge.Server(settings)
    discovered = server.handle(modern("server/discover"))["result"]
    assert bridge.MODERN_VERSION in discovered["supportedVersions"]
    assert discovered["resultType"] == "complete"
    assert discovered["_meta"][bridge.META_PREFIX + "serverInfo"] == bridge.SERVER_INFO
    assert len(server.handle(modern("tools/list"))["result"]["tools"]) == 4
    result = server.handle(modern("tools/call", {"name": "aria_status"}))["result"]
    assert result["structuredContent"]["unraid_version"] == "7.3.2"
    assert result["resultType"] == "complete"
    assert server.version is None and not server.initialized
    assert server.handle(modern("ping"))["result"]["resultType"] == "complete"


def test_modern_metadata_is_validated_on_every_request(settings):
    server = ready_server(settings)
    assert not server.handle(modern("tools/call", {"name": "aria_status"}))["result"]["isError"]
    bad_version = server.handle(modern("tools/list", version="2030-01-01"))
    assert bad_version["error"]["code"] == -32022
    assert bad_version["error"]["data"]["supported"] == list(bridge.PROTOCOL_VERSIONS)
    missing_capabilities = modern("tools/call", {"name": "aria_status"})
    del missing_capabilities["params"]["_meta"][bridge.META_PREFIX + "clientCapabilities"]
    assert server.handle(missing_capabilities)["error"]["code"] == -32602
    assert server.handle(rpc("server/discover"))["error"]["code"] == -32602
    # A modern request has not overwritten the separately negotiated legacy version.
    assert server.version == "2025-11-25"


def test_internal_errors_log_no_exception_details(settings, capsys):
    server = ready_server(settings)

    def fail(_settings):
        raise RuntimeError("secret=DO_NOT_RETURN")

    server.handlers["aria_status"] = fail
    result = server.handle(rpc("tools/call", {"name": "aria_status"}))
    assert result["result"]["isError"]
    assert "DO_NOT_RETURN" not in json.dumps(result)
    captured = capsys.readouterr()
    assert "DO_NOT_RETURN" not in captured.err
    assert not captured.out


def test_stdio_subprocess_exchanges_only_mcp_and_reads_environment(settings):
    messages = [
        initialize(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("tools/list", identifier="list"),
        rpc("tools/call", {"name": "aria_status"}, identifier="metrics"),
        rpc("ping", identifier=4),
    ]
    process = subprocess.run(
        [sys.executable, str(Path(bridge.__file__))],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "ARIA_HOST_ROOT": str(settings.host_root)},
    )
    assert process.returncode == 0
    assert not process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, "list", "metrics", 4]
    assert responses[2]["result"]["structuredContent"]["unraid_version"] == "7.3.2"


@pytest.mark.parametrize("line", [b"not-json\n", b'{"id":NaN}\n', b"\xff\n", b"[\n"])
def test_stdio_parse_errors_are_valid_bounded_json(settings, line):
    output = io.BytesIO()
    bridge.serve(io.BytesIO(line), output, settings)
    response = json.loads(output.getvalue())
    assert response["error"]["code"] == -32700
    assert response["id"] is None


def test_stdio_input_limit_closes_instead_of_draining(settings):
    stream = io.BytesIO(b"x" * (bridge.MAX_INPUT * 3))
    output = io.BytesIO()
    bridge.serve(stream, output, settings)
    assert stream.tell() == bridge.MAX_INPUT + 1
    assert json.loads(output.getvalue())["error"]["code"] == -32600


def test_stdio_output_limit(settings, monkeypatch):
    def too_large(self, message):
        return {"jsonrpc": "2.0", "id": 1, "result": "x" * bridge.MAX_OUTPUT}

    monkeypatch.setattr(bridge.Server, "handle", too_large)
    output = io.BytesIO()
    bridge.serve(io.BytesIO(b"{}\n"), output, settings)
    assert len(output.getvalue()) < bridge.MAX_OUTPUT
    assert json.loads(output.getvalue())["error"]["code"] == -32603
