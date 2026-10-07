"""Exercise real loopback HTTP, Unix-agent forwarding and the offline PHP CLI."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "tools/aria_bridge"
PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")


@pytest.fixture
def dashboard(tmp_path):
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError:
        probe.close()
        pytest.skip("Loopback HTTP binding is unavailable")
    port = probe.getsockname()[1]
    probe.close()
    unix_probe = None
    unix_available = True
    try:
        unix_probe = socket.socket(socket.AF_UNIX)
        unix_probe.bind(str(tmp_path / "probe.sock"))
    except OSError:
        unix_available = False
    finally:
        if unix_probe is not None:
            unix_probe.close()
    state = tmp_path / "management"
    agent_socket = tmp_path / "agent.sock"
    templates = tmp_path / "templates"
    templates.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    env = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_DASHBOARD_PORT": str(port),
        "ARIA_DASHBOARD_SOCKET": str(agent_socket),
        "ARIA_AGENT_TEMPLATES": str(templates),
        "ARIA_AGENT_SCRIPTS": str(scripts),
        "ARIA_AGENT_FILE_ROOTS": str(tmp_path),
    }
    processes = []
    logs = []
    source = BASE
    if not unix_available:
        # This sandbox forbids AF_UNIX. Keep the real HTTP/token/router surface
        # and substitute only its provider transport with the same dispatcher.
        # Native CI exercises the unmodified Unix-socket forwarding path.
        source = tmp_path / "router"
        source.mkdir()
        router = (BASE / "host-dashboard.php").read_text()
        router = router.replace(
            "require_once __DIR__.'/host-agent.php';",
            "require_once " + repr(str(BASE / "host-agent.php")) + ";",
        ).replace(
            "$reply = ariaDashboardForward(ariaDashboardRequest($body));",
            "$reply = ['ok' => true, 'result' => ariaDispatch($state, ariaDashboardRequest($body))];",
        )
        (source / "host-dashboard.php").write_text(router)
        (source / "dashboard-service.sh").write_text((BASE / "dashboard-service.sh").read_text())
    commands = []
    if unix_available:
        commands.append(
            ("agent", [PHP, str(BASE / "host-agent.php"), "serve", str(state), str(agent_socket)])
        )
    commands.append(("dashboard", ["bash", str(source / "dashboard-service.sh"), str(state)]))
    try:
        for kind, command in commands:
            log = (tmp_path / f"process-{len(processes)}.log").open("w+")
            logs.append(log)
            processes.append(subprocess.Popen(command, env=env, stdout=log, stderr=log))
            if kind == "agent":
                for _ in range(100):
                    if agent_socket.exists():
                        break
                    if processes[-1].poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.02)

        def request(path="/api/catalog", body=None, headers=None, method=None, auth=True):
            token_path = state / "dashboard-token"
            actual_headers = {"Host": f"127.0.0.1:{port}"}
            if auth and token_path.exists():
                actual_headers["Authorization"] = "Bearer " + token_path.read_text().strip()
            if body is not None:
                actual_headers["Content-Type"] = "application/json"
                body = json.dumps(body) if not isinstance(body, (str, bytes)) else body
            actual_headers.update(headers or {})
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                connection.request(
                    method or ("POST" if body is not None else "GET"), path, body, actual_headers
                )
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read().decode()
            finally:
                connection.close()

        for _ in range(150):
            try:
                if request("/", auth=False)[0] == 200:
                    break
            except OSError:
                pass
            if processes[-1].poll() is not None:
                logs[-1].seek(0)
                pytest.fail(logs[-1].read())
            time.sleep(0.02)
        else:
            pytest.fail("Local dashboard did not start")
        yield {
            "request": request,
            "state": state,
            "path": tmp_path,
            "env": env,
            "port": port,
            "unix_forwarded": unix_available,
        }
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


def test_landing_page_has_no_token_and_safe_browser_boundaries(dashboard):
    code, headers, body = dashboard["request"]("/", auth=False)
    token = (dashboard["state"] / "dashboard-token").read_text().strip()
    assert code == 200
    assert token not in body
    assert "localStorage" not in body and "sessionStorage" not in body
    assert "innerHTML" not in body
    assert "textContent" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "no-store" in headers["Cache-Control"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in headers
    assert (dashboard["state"] / "dashboard-token").stat().st_mode & 0o777 == 0o600


def test_public_health_exposes_only_service_identity_and_pid(dashboard):
    code, _, body = dashboard["request"]("/health", auth=False)
    result = json.loads(body)
    assert code == 200 and set(result) == {"service", "pid"}
    assert result["service"] == "aria-local-dashboard"
    assert isinstance(result["pid"], int) and result["pid"] > 0
    assert (
        dashboard["request"]("/health", headers={"Host": "evil.example:8786"}, auth=False)[0] == 403
    )


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer " + "0" * 64])
def test_api_requires_token_for_every_request(dashboard, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    code, _, _ = dashboard["request"](headers=headers, auth=False)
    assert code == 401
    code, _, _ = dashboard["request"](
        "/api/dispatch", {"action": "capabilities", "arguments": {}}, headers=headers, auth=False
    )
    assert code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example:8786"},
        {"Host": "127.0.0.1.evil.example:8786"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_authenticated_requests_still_reject_rebinding_and_csrf(dashboard, headers):
    assert dashboard["request"](headers=headers)[0] == 403
    assert dashboard["request"]("/", headers=headers, auth=False)[0] == 403


def test_same_origin_catalog_and_validated_capabilities(dashboard):
    headers = {"Origin": f"http://127.0.0.1:{dashboard['port']}", "Sec-Fetch-Site": "same-origin"}
    code, _, body = dashboard["request"](headers=headers)
    assert code == 200
    actions = json.loads(body)["result"]["actions"]
    assert {
        "capabilities",
        "script_run",
        "operations_host_exec",
        "operations_script_save",
    }.issubset({item["action"] for item in actions})
    code, _, body = dashboard["request"](
        "/api/dispatch", {"action": "capabilities", "arguments": {}}, headers=headers
    )
    assert code == 200
    assert json.loads(body)["result"]["queue"]


def test_mutation_queue_is_durable_idempotent_and_never_executed_in_http_process(dashboard):
    marker = dashboard["path"] / "not-executed"
    body = {
        "action": "operations_host_exec",
        "arguments": {"argv": ["/usr/bin/touch", str(marker)], "request_id": "dashboard-test"},
    }
    code, _, text = dashboard["request"]("/api/dispatch", body)
    assert code == 200
    first = json.loads(text)["result"]
    assert first["status"] == "queued"
    _, _, text = dashboard["request"]("/api/dispatch", body)
    second = json.loads(text)["result"]
    assert second["job_id"] == first["job_id"] and second["deduplicated"]
    assert not marker.exists()
    job_path = dashboard["state"] / "jobs" / (first["job_id"] + ".json")
    assert job_path.exists() and job_path.stat().st_mode & 0o777 == 0o600
    code, _, text = dashboard["request"](
        "/api/dispatch", {"action": "job_status", "arguments": {"job_id": first["job_id"]}}
    )
    result = json.loads(text)["result"]
    assert code == 200 and result["status"] == "queued"
    assert "arguments" not in result
    body["arguments"]["argv"] = ["/bin/true"]
    assert dashboard["request"]("/api/dispatch", body)[0] == 422


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"action": "capabilities", "arguments": []},
        {"action": "capabilities", "arguments": {}, "extra": True},
        {"action": "operations_host_exec", "arguments": {"argv": ["/bin/true"]}},
        {
            "action": "operations_host_exec",
            "arguments": {"argv": "shell text", "request_id": "bad"},
        },
        "not json",
    ],
)
def test_invalid_requests_never_create_jobs(dashboard, body):
    assert dashboard["request"]("/api/dispatch", body)[0] == 422
    assert not list((dashboard["state"] / "jobs").glob("*.json"))


def test_api_enforces_method_content_type_and_body_size(dashboard):
    assert dashboard["request"]("/api/dispatch")[0] == 405
    assert dashboard["request"]("/api/catalog", method="OPTIONS")[0] == 405
    assert dashboard["request"]("/api/dispatch", "{}", {"Content-Type": "text/plain"})[0] == 415
    assert dashboard["request"]("/api/dispatch", "x" * (1048576 + 1))[0] == 413


def test_dashboard_cannot_serve_source_or_read_token_via_managed_file_api(dashboard):
    token = (dashboard["state"] / "dashboard-token").read_text().strip()
    for path in (
        "/host-agent.php",
        "/dashboard-token",
        "/../management/dashboard-token",
        "/api/catalog?token=" + token,
    ):
        code, _, body = dashboard["request"](path)
        assert code == 404 and token not in body
    code, _, body = dashboard["request"](
        "/api/dispatch",
        {
            "action": "operations_file_read",
            "arguments": {"path": str(dashboard["state"] / "dashboard-token")},
        },
    )
    assert code == 422 and token not in body


def test_token_is_retained_on_restart_and_symlinks_are_rejected(dashboard):
    token_file = dashboard["state"] / "dashboard-token"
    previous = token_file.read_text()
    command = [PHP, str(BASE / "host-dashboard.php"), "init", str(dashboard["state"])]
    result = subprocess.run(
        command, env=dashboard["env"], capture_output=True, text=True, check=True
    )
    assert token_file.read_text() == previous
    assert previous.strip() not in result.stdout + result.stderr
    token_file.unlink()
    target = dashboard["path"] / "outside"
    target.write_text("a" * 64 + "\n")
    token_file.symlink_to(target)
    result = subprocess.run(
        command, env=dashboard["env"], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert target.read_text() == "a" * 64 + "\n"


def test_cli_catalog_dispatch_queue_and_invalid_input_without_http(tmp_path):
    state = tmp_path / "state"
    env = {**os.environ, "ARIA_AGENT_TEST_MODE": "1"}
    command = [PHP, str(BASE / "aria-local.php"), str(state)]
    catalog = subprocess.run(
        command + ["--catalog"], env=env, capture_output=True, text=True, check=True
    )
    assert "operations_host_exec" in catalog.stdout
    assert not (state / "dashboard-token").exists()
    result = subprocess.run(
        command + ["capabilities"], env=env, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout)["result"]["queue"]
    arguments = {"argv": ["/bin/true"], "request_id": "offline-cli"}
    result = subprocess.run(
        command + ["operations_host_exec", "-"],
        input=json.dumps(arguments),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["result"]["status"] == "queued"
    invalid = subprocess.run(
        command + ["capabilities", "[]"], env=env, capture_output=True, text=True, check=False
    )
    assert invalid.returncode == 1 and not json.loads(invalid.stdout)["ok"]


def test_production_listener_binding_cannot_be_overridden():
    source = (BASE / "dashboard-service.sh").read_text()
    assert '"127.0.0.1:$aria_dashboard_port"' in source
    assert '"${ARIA_AGENT_TEST_MODE:-}" == 1 && -n "${ARIA_DASHBOARD_PORT:-}"' in source
    assert "aria_dashboard_port=8786" in source
    assert "0.0.0.0" not in source


def test_native_unix_agent_forwarding(dashboard):
    if not dashboard["unix_forwarded"]:
        pytest.skip("Unix sockets are unavailable; other HTTP tests substitute only the transport")
    code, _, body = dashboard["request"](
        "/api/dispatch", {"action": "capabilities", "arguments": {}}
    )
    assert code == 200 and json.loads(body)["result"]["queue"]
