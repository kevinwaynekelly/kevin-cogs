"""Actual Unix socket and MCP boundary checks for opt-in host management."""

import hashlib
import io
import json
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.aria_bridge import management, server


@pytest.fixture
def agent_peer(tmp_path, monkeypatch):
    peer = SimpleNamespace(
        body=b'{"ok":true,"result":{"job_id":"request-1","status":"queued"}}\n',
        requests=[],
        delay=0,
        responder=None,
    )

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = json.loads(self.rfile.readline(management.MAX_REQUEST_BYTES + 1))
            peer.requests.append(request)
            if peer.delay:
                time.sleep(peer.delay)
            try:
                self.wfile.write(peer.responder(request) if peer.responder else peer.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    peer.path = str(tmp_path / "agent.sock")
    try:
        service = socketserver.UnixStreamServer(peer.path, Handler)
    except PermissionError:
        # Some test runtimes permit local TCP but prohibit Unix binds. Exercise the
        # identical framing and deadline code over TCP, checking the configured
        # Unix socket address at the adapter boundary instead of skipping coverage.
        service = socketserver.TCPServer(("127.0.0.1", 0), Handler)

        class SocketAdapter:
            def __init__(self, family, kind):
                assert family == socket.AF_UNIX and kind == socket.SOCK_STREAM
                self.connection = socket.socket(socket.AF_INET, kind)

            def connect(self, path):
                assert path == peer.path
                self.connection.connect(service.server_address)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.connection.close()

            def __getattr__(self, name):
                return getattr(self.connection, name)

        monkeypatch.setattr(
            management,
            "socket",
            SimpleNamespace(
                AF_UNIX=socket.AF_UNIX,
                SOCK_STREAM=socket.SOCK_STREAM,
                socket=SocketAdapter,
            ),
        )
    worker = threading.Thread(target=lambda: service.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield peer
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=2)


def modern_call(name, arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": "call",
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": {} if arguments is None else arguments,
            "_meta": {
                server.META_PREFIX + "protocolVersion": server.MODERN_VERSION,
                server.META_PREFIX + "clientCapabilities": {},
            },
        },
    }


def call_agent(peer, name, arguments=None):
    bridge = server.Server(server.Settings(agent_socket=peer.path))
    return bridge.handle(modern_call(name, arguments))


def test_all_management_tools_are_discoverable_and_mutations_deduplicated():
    definitions = {definition["name"]: definition for definition in server.tool_definitions()}
    assert len(management.TOOLS) == 18
    assert len(definitions) == 22
    assert server.SERVER_INFO["version"] == "1.2.0"
    for spec in management.TOOL_SPECS:
        definition = definitions[spec.name]
        assert definition["inputSchema"]["additionalProperties"] is False
        assert definition["annotations"]["readOnlyHint"] == spec.read_only
        assert definition["annotations"]["idempotentHint"]
        if not spec.read_only:
            assert "request_id" in definition["inputSchema"]["required"]
            assert definition["annotations"]["destructiveHint"]
    assert not any(
        "command" in spec.properties or "path" in spec.properties for spec in management.TOOL_SPECS
    )


def test_disabled_management_is_explicit_and_legacy_status_configuration_is_unchanged(monkeypatch):
    monkeypatch.delenv("ARIA_AGENT_SOCKET", raising=False)
    assert server.Settings.from_environment().agent_socket == ""
    result = server.Server(server.Settings()).handle(modern_call("aria_scripts"))["result"]
    assert result["isError"]
    assert "ARIA_AGENT_SOCKET" in result["structuredContent"]["error"]
    monkeypatch.setenv("ARIA_AGENT_SOCKET", "/run/aria-management/agent.sock")
    assert server.Settings.from_environment().agent_socket == "/run/aria-management/agent.sock"


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_tools_have_fixed_action_and_return_job_acceptance(agent_peer, action):
    response = call_agent(
        agent_peer,
        "aria_container_" + action,
        {"name": "red-discordbot", "request_id": "request-1"},
    )
    assert not response["result"]["isError"]
    assert response["result"]["structuredContent"] == {"job_id": "request-1", "status": "queued"}
    assert agent_peer.requests == [
        {
            "action": "container_action",
            "arguments": {
                "name": "red-discordbot",
                "request_id": "request-1",
                "action": action,
            },
        }
    ]


@pytest.mark.parametrize(
    ("name", "arguments", "action", "expected"),
    [
        ("aria_management_capabilities", {}, "capabilities", {}),
        ("aria_container_inspect", {"name": "Plex"}, "container_inspect", {"name": "Plex"}),
        ("aria_container_logs", {"name": "Plex"}, "container_logs", {"name": "Plex", "tail": 100}),
        ("aria_templates", {}, "templates_list", {}),
        (
            "aria_template_read",
            {"template": "my-Plex.xml"},
            "template_get",
            {"template": "my-Plex.xml"},
        ),
        ("aria_scripts", {}, "scripts_list", {}),
        (
            "aria_script_read",
            {"name": "Aria GPT status"},
            "script_get",
            {"name": "Aria GPT status"},
        ),
        ("aria_job_status", {"job_id": "request-1"}, "job_status", {"job_id": "request-1"}),
        (
            "aria_container_update",
            {"name": "Plex", "request_id": "r1"},
            "container_update",
            {"name": "Plex", "request_id": "r1"},
        ),
        (
            "aria_containers_update_all",
            {"request_id": "r1"},
            "containers_update_all",
            {"request_id": "r1"},
        ),
        (
            "aria_bridge_update",
            {"request_id": "bridge1"},
            "bridge_update",
            {"request_id": "bridge1"},
        ),
        ("aria_bridge_update_status", {}, "bridge_update_status", {}),
    ],
)
def test_named_tools_map_only_to_fixed_host_operations(
    agent_peer, name, arguments, action, expected
):
    assert not call_agent(agent_peer, name, arguments)["result"]["isError"]
    assert agent_peer.requests == [{"action": action, "arguments": expected}]


def test_template_save_and_deploy_are_separate_and_version_checked(agent_peer):
    arguments = {
        "template": "my-Plex.xml",
        "xml": "<Container/>",
        "expected_sha256": "",
        "request_id": "new-template",
    }
    assert not call_agent(agent_peer, "aria_template_save", arguments)["result"]["isError"]
    assert agent_peer.requests[-1] == {"action": "template_save", "arguments": arguments}
    arguments = {
        "template": "my-Plex.xml",
        "expected_sha256": "a" * 64,
        "request_id": "deploy-template",
    }
    assert not call_agent(agent_peer, "aria_template_deploy", arguments)["result"]["isError"]
    assert agent_peer.requests[-1] == {
        "action": "template_deploy",
        "arguments": {
            **arguments,
            "pull": True,
            "start": True,
        },
    }
    arguments.update(pull=False, start=False)
    assert not call_agent(agent_peer, "aria_template_deploy", arguments)["result"]["isError"]
    assert agent_peer.requests[-1]["arguments"] == arguments


def test_script_run_requires_hash_and_sets_bounded_timeout(agent_peer):
    arguments = {
        "name": "3_rclone_flash_backups",
        "expected_sha256": "a" * 64,
        "request_id": "script1",
    }
    assert not call_agent(agent_peer, "aria_script_run", arguments)["result"]["isError"]
    assert agent_peer.requests == [
        {
            "action": "script_run",
            "arguments": {
                **arguments,
                "timeout_seconds": 3600,
            },
        }
    ]


def test_containers_prefers_live_management_over_stale_snapshot_and_docker_socket(agent_peer):
    live = {
        "containers": [
            {
                "Names": "Plex",
                "State": "running",
                "Image": "plex:latest",
                "Status": "Up",
                "Labels": {"secret": "SECRET"},
            }
        ],
        "checked_at": "2026-10-07T04:00:00Z",
    }
    agent_peer.body = json.dumps({"ok": True, "result": live}).encode() + b"\n"
    settings = replace(
        server.Settings(),
        agent_socket=agent_peer.path,
        docker_snapshot_file="/absent/snapshot",
        docker_socket="/absent/docker.sock",
    )
    assert server.containers(settings) == {
        "containers": [
            {"names": ["Plex"], "state": "running", "image": "plex:latest", "status": "Up"}
        ],
        "source": "host-agent",
        "total": 1,
        "truncated": False,
        "checked_at": live["checked_at"],
    }
    assert agent_peer.requests == [{"action": "containers", "arguments": {}}]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("aria_scripts", {"command": "SECRET"}),
        ("aria_bridge_update", {}),
        ("aria_bridge_update", {"request_id": "r1", "branch": "other"}),
        ("aria_bridge_update", {"request_id": "r1", "command": "SECRET"}),
        ("aria_bridge_update_status", {"job_id": "r1"}),
        ("aria_container_start", {"name": "Plex"}),
        ("aria_container_start", {"name": "Plex", "request_id": "r1", "action": "rm"}),
        ("aria_container_start", {"name": "Plex", "request_id": "r1\n"}),
        ("aria_container_inspect", {"name": "../Plex"}),
        ("aria_container_inspect", {"name": "--help"}),
        ("aria_container_inspect", {"name": "Plex;SECRET"}),
        ("aria_template_read", {"template": "/boot/my-Plex.xml"}),
        ("aria_template_read", {"template": "../my-Plex.xml"}),
        ("aria_template_read", {"template": "my-Plex.txt"}),
        ("aria_script_read", {"name": "../../SECRET"}),
        ("aria_container_logs", {"name": "Plex", "tail": True}),
        ("aria_container_logs", {"name": "Plex", "tail": 501}),
        ("aria_container_logs", {"name": "Plex", "tail": 0}),
        (
            "aria_template_deploy",
            {"template": "my-Plex.xml", "expected_sha256": "", "request_id": "r1"},
        ),
        (
            "aria_template_deploy",
            {
                "template": "my-Plex.xml",
                "expected_sha256": "a" * 64,
                "request_id": "r1",
                "start": 1,
            },
        ),
        (
            "aria_script_run",
            {
                "name": "backup",
                "expected_sha256": "a" * 64,
                "request_id": "r1",
                "timeout_seconds": 86401,
            },
        ),
        (
            "aria_script_run",
            {
                "name": "backup",
                "expected_sha256": "a" * 64,
                "request_id": "r1",
                "timeout_seconds": 0,
            },
        ),
        ("aria_job_status", {"job_id": "a" * 65}),
    ],
)
def test_invalid_arguments_are_rejected_before_host_connection(agent_peer, tool, arguments):
    response = call_agent(agent_peer, tool, arguments)
    assert response["error"]["code"] == -32602
    assert "SECRET" not in json.dumps(response)
    assert not agent_peer.requests


@pytest.mark.parametrize("arguments", [None, [], "SECRET", True])
def test_arguments_must_be_an_object(agent_peer, arguments):
    message = modern_call("aria_scripts")
    message["params"]["arguments"] = arguments
    response = server.Server(server.Settings(agent_socket=agent_peer.path)).handle(message)
    assert response["error"]["code"] == -32602
    assert not agent_peer.requests


@pytest.mark.parametrize(
    "xml",
    [
        "x" * (management.MAX_XML_BYTES + 1),
        "é" * (management.MAX_XML_BYTES // 2 + 1),
        "\ud800",
        "\x00",
    ],
)
def test_xml_limits_use_utf8_bytes_and_reject_invalid_unicode(agent_peer, xml):
    response = call_agent(
        agent_peer,
        "aria_template_save",
        {
            "template": "my-Plex.xml",
            "xml": xml,
            "expected_sha256": "",
            "request_id": "r1",
        },
    )
    assert response["error"]["code"] == -32602
    assert not agent_peer.requests


def test_stdio_can_carry_large_template_xml(agent_peer):
    xml = "<Container><Description>" + 'é"\\' * 10000 + "</Description></Container>"
    message = modern_call(
        "aria_template_save",
        {
            "template": "my-Plex.xml",
            "xml": xml,
            "expected_sha256": "",
            "request_id": "large1",
        },
    )
    encoded = json.dumps(message).encode() + b"\n"
    assert len(encoded) > 16 * 1024
    output = io.BytesIO()
    server.serve(io.BytesIO(encoded), output, server.Settings(agent_socket=agent_peer.path))
    assert not json.loads(output.getvalue())["result"]["isError"]
    assert agent_peer.requests[0]["arguments"]["xml"] == xml


@pytest.mark.parametrize(
    "body",
    [
        b'{"ok":true,"result":{}}',
        b'{"ok":true,"result":{}}\n{}\n',
        b'{"ok":true,"result":[],"secret":"SECRET"}\n',
        b'{"ok":true,"ok":false,"result":{}}\n',
        b'{"ok":1,"result":{}}\n',
        b'{"ok":true,"result":{"value":NaN}}\n',
        b"\xff\n",
    ],
)
def test_invalid_host_response_fails_without_echoing_provider_text(agent_peer, body):
    agent_peer.body = body
    response = call_agent(agent_peer, "aria_scripts")
    assert response["result"]["isError"]
    assert "invalid response" in response["result"]["structuredContent"]["error"]
    assert "SECRET" not in json.dumps(response)


@pytest.mark.parametrize(
    "error",
    [
        "hash mismatch",
        "request_id conflict",
        "job unknown",
        "management busy",
        "bridge update unavailable",
        "SECRET arbitrary exception",
    ],
)
def test_host_error_allowlist_is_fixed_and_preserves_useful_failures(agent_peer, error):
    agent_peer.body = json.dumps({"ok": False, "error": error}).encode() + b"\n"
    response = call_agent(agent_peer, "aria_scripts")
    assert response["result"]["isError"]
    assert "SECRET" not in json.dumps(response)
    if error in management.SAFE_HOST_ERRORS:
        assert error in response["result"]["structuredContent"]["error"]


def test_oversized_response_is_bounded(agent_peer):
    agent_peer.body = b"x" * (management.MAX_RESPONSE_BYTES + 100)
    response = call_agent(agent_peer, "aria_scripts")
    assert response["result"]["isError"]
    assert "exceeds the bridge limit" in response["result"]["structuredContent"]["error"]


@pytest.mark.parametrize(
    ("name", "arguments", "status_tool"),
    [
        ("aria_container_restart", {"name": "Plex", "request_id": "uncertain1"}, "aria_job_status"),
        ("aria_bridge_update", {"request_id": "uncertain1"}, "aria_bridge_update_status"),
    ],
)
def test_uncertain_mutation_timeout_is_never_retried(
    agent_peer, monkeypatch, name, arguments, status_tool
):
    monkeypatch.setattr(management, "REQUEST_TIMEOUT", 0.03)
    agent_peer.delay = 0.1
    response = call_agent(agent_peer, name, arguments)
    assert response["result"]["isError"]
    assert "same request_id" in response["result"]["structuredContent"]["error"]
    assert status_tool in response["result"]["structuredContent"]["error"]
    if name == "aria_bridge_update":
        assert "aria_job_status" not in response["result"]["structuredContent"]["error"]
    assert len(agent_peer.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        b'{"ok":true,"result":{}}',
        b"x" * (management.MAX_RESPONSE_BYTES + 1),
    ],
)
def test_uncertain_bridge_update_response_points_to_its_separate_status_tool(agent_peer, body):
    agent_peer.body = body
    response = call_agent(agent_peer, "aria_bridge_update", {"request_id": "bridge1"})
    result = response["result"]
    assert result["isError"]
    assert "same request_id" in result["structuredContent"]["error"]
    assert "aria_bridge_update_status" in result["structuredContent"]["error"]
    assert "aria_job_status" not in result["structuredContent"]["error"]


def test_unsupported_host_action_cannot_connect(agent_peer):
    with pytest.raises(management.ManagementError, match="not supported"):
        management.request(agent_peer.path, "shell", {"command": "SECRET"})
    assert not agent_peer.requests


def test_notifications_cannot_execute_management_jobs(agent_peer):
    message = modern_call("aria_containers_update_all", {"request_id": "request-1"})
    del message["id"]
    assert server.Server(server.Settings(agent_socket=agent_peer.path)).handle(message) is None
    assert not agent_peer.requests


def test_python_tools_interoperate_with_actual_php_dispatch_and_durable_jobs(agent_peer, tmp_path):
    """Actual PHP parsing, redaction, hash values and job receipts across the client boundary."""
    php = shutil.which("php")
    if php is None:
        pytest.skip("Optional Unraid host-agent integration requires PHP")
    probe = subprocess.run(
        [php, "-r", 'echo function_exists("simplexml_load_string") ? "yes" : "no";'],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if probe.stdout != "yes":
        pytest.skip("Optional Unraid host-agent integration requires PHP SimpleXML")
    host_source = Path(server.__file__).with_name("host-agent.php")
    templates = tmp_path / "templates"
    scripts = tmp_path / "scripts"
    state = tmp_path / "state"
    templates.mkdir()
    (scripts / "Fixture backup").mkdir(parents=True)
    source_xml = (
        '<Container version="2"><Name>Fixture</Name><Repository>fixture:latest</Repository>'
        "<Description>Original description</Description>"
        '<Config Name="API_KEY" Target="API_KEY" Type="Variable">DUMMY_SECRET</Config>'
        "</Container>"
    )
    (templates / "my-Fixture.xml").write_text(source_xml)
    script = '#!/bin/bash\nprintf "fixture only\\n"\n'
    (scripts / "Fixture backup" / "script").write_text(script)
    docker = tmp_path / "fake-docker"
    docker.write_text(
        "#!/bin/sh\n"
        'test "$1" = "ps" || exit 1\n'
        "printf '%s\\n' "
        '\'{"Names":"Fixture","Image":"fixture:latest","State":"running","Status":"Up"}\'\n'
    )
    docker.chmod(0o700)
    environment = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_AGENT_TEMPLATES": str(templates),
        "ARIA_AGENT_SCRIPTS": str(scripts),
        "ARIA_AGENT_DOCKER": str(docker),
    }
    php_dispatch = (
        "require $argv[1]; ariaInit($argv[2]); "
        'try {$reply = ["ok"=>true,"result"=>ariaDispatch($argv[2],'
        "json_decode(stream_get_contents(STDIN),true))];} "
        'catch(Throwable $e) {$reply = ["ok"=>false,"error"=>ariaError($e)];} '
        'echo ariaJson($reply)."\\n";'
    )

    def responder(request):
        process = subprocess.run(
            [php, "-d", "display_errors=0", "-r", php_dispatch, str(host_source), str(state)],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=5,
            env=environment,
        )
        assert process.returncode == 0
        assert not process.stderr
        return process.stdout

    agent_peer.responder = responder

    def tool(name, arguments=None):
        result = call_agent(agent_peer, name, arguments)["result"]
        assert not result["isError"], result
        return result["structuredContent"]

    assert tool("aria_management_capabilities")["requires_request_id"] is True
    live = tool("aria_containers")
    assert live["source"] == "host-agent"
    assert live["containers"][0]["names"] == ["Fixture"]
    assert live["total"] == 1
    templates_result = tool("aria_templates")
    assert templates_result["templates"][0]["template"] == "my-Fixture.xml"
    read = tool("aria_template_read", {"template": "my-Fixture.xml"})
    assert read["sha256"] == hashlib.sha256(source_xml.encode()).hexdigest()
    assert "DUMMY_SECRET" not in json.dumps(read)
    assert "__ARIA_REDACTED_" in read["xml"]
    save_args = {
        "template": "my-Fixture.xml",
        "xml": read["xml"].replace("Original description", "Updated description"),
        "expected_sha256": read["sha256"],
        "request_id": "interop-save-1",
    }
    job = tool("aria_template_save", save_args)
    assert job["status"] == "queued" and job["deduplicated"] is False
    repeat = tool("aria_template_save", save_args)
    assert repeat["job_id"] == job["job_id"] and repeat["deduplicated"] is True
    status = tool("aria_job_status", {"job_id": job["job_id"]})
    assert status["action"] == "template_save" and status["status"] == "queued"
    assert "arguments" not in status and "fingerprint" not in status
    conflict = call_agent(
        agent_peer, "aria_template_save", {**save_args, "expected_sha256": "a" * 64}
    )
    assert conflict["result"]["isError"]
    assert "request_id conflict" in conflict["result"]["structuredContent"]["error"]
    assert len(list((state / "jobs").glob("*.json"))) == 1
    assert (templates / "my-Fixture.xml").read_text() == source_xml
    scripts_result = tool("aria_scripts")
    assert scripts_result["scripts"][0]["name"] == "Fixture backup"
    script_read = tool("aria_script_read", {"name": "Fixture backup"})
    assert script_read["script"] == script
    assert script_read["sha256"] == hashlib.sha256(script.encode()).hexdigest()
    script_job = tool(
        "aria_script_run",
        {
            "name": "Fixture backup",
            "expected_sha256": script_read["sha256"],
            "request_id": "interop-script-1",
        },
    )
    assert script_job["status"] == "queued"
    saved_job = json.loads((state / "jobs" / (script_job["job_id"] + ".json")).read_text())
    assert saved_job["arguments"]["expected_sha256"] == script_read["sha256"]
    assert saved_job["arguments"]["timeout_seconds"] == 3600
    # The integration exercises queue acceptance, never runs the script or worker.
    assert saved_job["started_at"] is None
