"""Real PHP application boundaries against local, mocked HTTP providers.

These do not contact Kevin's applications or validate a live Unraid deployment.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "tools/aria_bridge/host-agent.php"
APPLICATIONS = ROOT / "tools/aria_bridge/host-applications.php"
PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")
TOKEN = "SECRET_APPLICATION_TOKEN_456"
PASSWORD = "SECRET_QBIT_PASSWORD_789"
SID = "privateSID123"
RUNNER = r"""
require $argv[1];
require_once $argv[2];
require_once dirname($argv[1]).'/host-operations.php';
$a=json_decode(stream_get_contents(STDIN),true);
ariaInit($a['state']);
try {
    if ($a['mode']==='dispatch') $r=ariaDispatch($a['state'],['action'=>$a['action'],'arguments'=>$a['arguments']]);
    elseif ($a['mode']==='write') $r=ariaApplicationsExecute($a['state'],$a['job']);
    elseif ($a['mode']==='activity') $r=ariaApplicationsActivity($a['state'],$a['arguments']['profiles']);
    else $r=ariaApplicationsRead($a['state'],$a['action'],$a['arguments']);
    echo ariaJson(['ok'=>true,'result'=>$r]);
} catch (Throwable $e) { echo ariaJson(['ok'=>false,'error'=>$e->getMessage()]); }
"""


@pytest.fixture
def api():
    calls = []
    replies = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body.decode(),
                }
            )
            path = urlsplit(self.path).path
            reply = replies.get(path)
            if reply is None:
                if path == "/api/v2/auth/login":
                    reply = (200, "Ok.", {"Set-Cookie": f"SID={SID}; path=/; HttpOnly"})
                elif path == "/api/v2/auth/logout":
                    reply = (200, "", {})
                elif path == "/api/v2/app/version":
                    reply = (200, "v5.1.0", {})
                else:
                    reply = (200, {}, {})
            status, value, headers = reply
            raw = json.dumps(value).encode() if not isinstance(value, str) else value.encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("Local HTTP sockets unavailable")
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", replies, calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def agent(tmp_path, api):
    state = tmp_path / "state"
    state.mkdir()
    (state / "secrets").mkdir()
    for name, value in {
        "arrkey": TOKEN,
        "plexkey": TOKEN,
        "qbitkey": json.dumps({"username": "admin", "password": PASSWORD}),
    }.items():
        (state / "secrets" / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "revision": "a" * 32,
                    "value": value,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            )
        )
    counter = 0

    def call(action, arguments=None, *, write=False, activity=False, dispatch=False):
        nonlocal counter
        counter += 1
        payload = {
            "state": str(state),
            "arguments": arguments or {},
            "action": action,
            "mode": "dispatch" if dispatch else ("activity" if activity else "read"),
        }
        if write:
            payload.update(
                mode="write",
                job={"job_id": f"job-{counter}", "action": action, "arguments": arguments or {}},
            )
        result = subprocess.run(
            [PHP, "-r", RUNNER, str(HOST), str(APPLICATIONS)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env={**os.environ, "ARIA_AGENT_TEST_MODE": "1"},
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert not result.stderr, result.stderr
        return json.loads(result.stdout)

    def save(kind="sonarr", name="app", **kwargs):
        config = {
            "profile": name,
            "type": kind,
            "base_url": api[0],
            "secret_ref": "qbitkey" if kind == "qbittorrent" else "arrkey",
            "expected_sha256": "",
        }
        config.update(kwargs)
        reply = call("applications_profile_save", config, write=True)
        return reply

    return call, save, state


def test_profile_creation_roundtrip_backup_and_hash_gate(agent):
    call, save, state = agent
    created = save()
    assert created["ok"], created
    digest = created["result"]["sha256"]
    profile = call("applications_profile_get", {"profile": "app"})["result"]
    assert profile["sha256"] == digest
    assert profile["verify_tls"] is True
    assert profile["secret_ref"] == "arrkey"
    assert TOKEN not in json.dumps(profile)
    assert (state / "applications/app.json").stat().st_mode & 0o777 == 0o600
    rejected = save(expected_sha256="b" * 64)
    assert rejected["error"] == "hash mismatch"
    changed = save(expected_sha256=digest, base_url="https://aria.example/sonarr")
    assert changed["ok"] and changed["result"]["backup_created"]
    assert len(list((state / "backups").glob("*-application.json"))) == 1
    deleted = call(
        "applications_profile_delete",
        {"profile": "app", "expected_sha256": changed["result"]["sha256"]},
        write=True,
    )
    assert deleted["ok"]
    assert call("applications_profiles")["result"]["profiles"] == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/shadow",
        "https://admin:secret@example.com",
        "https://example.com?token=x",
        "https://example.com/#x",
        "http://example.com/%2e%2e",
        "http://example.com/../x",
        "http://example.com\\@localhost",
        "http://example.com/\r\nX-Token:x",
    ],
)
def test_profile_rejects_credential_urls_and_ambiguous_paths(agent, url):
    _, save, _ = agent
    assert save(base_url=url)["error"] == "invalid request"


def test_profile_invalid_credentials_do_not_save(agent):
    _, save, state = agent
    assert save(secret_ref="absent")["error"] == "application credential unavailable"
    assert not (state / "applications/app.json").exists()
    assert save("qbittorrent", secret_ref="arrkey")["error"] == "application credential unavailable"


def test_symlink_profile_not_followed(agent, tmp_path):
    call, _, state = agent
    directory = state / "applications"
    directory.mkdir()
    target = tmp_path / "secret.json"
    target.write_text("private")
    (directory / "app.json").symlink_to(target)
    assert not call("applications_profile_get", {"profile": "app"})["ok"]
    assert target.read_text() == "private"


def test_arr_queue_keeps_diagnostic_fields_without_tokens_or_urls(agent, api):
    call, save, _ = agent
    assert save()["ok"]
    api[1]["/api/v3/queue"] = (
        200,
        {
            "totalRecords": 1,
            "records": [
                {
                    "id": 42,
                    "title": "Episode",
                    "downloadId": "a" * 40,
                    "statusMessages": [
                        {
                            "title": "Import blocked",
                            "messages": [
                                f"Unable to import {TOKEN} https://tracker.example/passkey/unknown"
                            ],
                        }
                    ],
                    "downloadUrl": "https://tracker.example/private",
                    "apiKey": TOKEN,
                }
            ],
        },
        {},
    )
    reply = call("applications_query", {"profile": "app", "operation": "queue"})
    assert reply["ok"], reply
    assert reply["result"]["data"]["total_records"] == 1
    row = reply["result"]["data"]["items"][0]
    assert row["downloadId"] == "a" * 40
    serialized = json.dumps(reply)
    assert (
        TOKEN not in serialized
        and "tracker.example" not in serialized
        and "apiKey" not in serialized
    )
    assert api[2][-1]["headers"]["X-Api-Key"] == TOKEN
    assert "pageSize=50" in api[2][-1]["path"]


@pytest.mark.parametrize(
    "status,error",
    [
        (301, "application redirect refused"),
        (302, "application redirect refused"),
        (401, "application authentication failed"),
        (403, "application authentication failed"),
        (404, "application operation unavailable"),
        (500, "application request failed"),
    ],
)
def test_http_errors_never_follow_redirects_or_echo_provider_body(agent, api, status, error):
    call, save, _ = agent
    assert save()["ok"]
    api[1]["/api/v3/system/status"] = (status, TOKEN, {"Location": api[0] + "/secret-destination"})
    reply = call("applications_query", {"profile": "app", "operation": "status"})
    assert reply == {"ok": False, "error": error}
    assert len(api[2]) == 1


def test_oversized_reply_rejected(agent, api):
    call, save, _ = agent
    save()
    api[1]["/api/v3/health"] = (200, " " * 524289, {})
    assert (
        call("applications_query", {"profile": "app", "operation": "health"})["error"]
        == "application response too large"
    )


@pytest.mark.parametrize(
    "kind,operation,ids,expected",
    [
        ("sonarr", "refresh", [12], {"name": "RefreshSeries", "seriesId": 12}),
        ("sonarr", "rescan", [13], {"name": "RescanSeries", "seriesId": 13}),
        ("sonarr", "search", [14], {"name": "SeriesSearch", "seriesId": 14}),
        ("sonarr", "rss_sync", None, {"name": "RssSync"}),
        ("radarr", "refresh", [12], {"name": "RefreshMovie", "movieId": 12}),
        ("radarr", "rescan", [13], {"name": "RescanMovie", "movieId": 13}),
        ("radarr", "search", [12, 14], {"name": "MoviesSearch", "movieIds": [12, 14]}),
    ],
)
def test_arr_commands_are_typed_and_targeted(agent, api, kind, operation, ids, expected):
    call, save, _ = agent
    saved = save(kind)["result"]
    api[1]["/api/v3/command"] = (
        201,
        {"id": 9, "name": expected["name"], "status": "queued", "body": {"token": TOKEN}},
        {},
    )
    args = {"profile": "app", "expected_sha256": saved["sha256"], "operation": operation}
    if ids is not None:
        args["ids"] = ids
    reply = call("applications_command", args, write=True)
    assert reply["ok"], reply
    assert reply["result"]["command"]["id"] == 9
    assert reply["result"]["application_completion_verified"] is False
    assert json.loads(api[2][-1]["body"]) == expected
    assert api[2][-1]["method"] == "POST"
    assert TOKEN not in json.dumps(reply)


def test_changed_profile_rejects_queued_command_before_http(agent, api):
    call, save, _ = agent
    save()
    reply = call(
        "applications_command",
        {"profile": "app", "expected_sha256": "b" * 64, "operation": "rss_sync"},
        write=True,
    )
    assert reply["error"] == "hash mismatch"
    assert api[2] == []


def test_sonarr_v5_prefix_and_radarr_v5_rejected(agent, api):
    call, save, _ = agent
    assert save(api_version="v5")["ok"]
    api[1]["/api/v5/health"] = (200, [], {})
    assert call("applications_query", {"profile": "app", "operation": "health"})["ok"]
    assert api[2][-1]["path"] == "/api/v5/health"
    assert save("radarr", name="radarr", api_version="v5")["error"] == "invalid request"


def test_plex_sessions_json_projection_and_xml_fallback(agent, api):
    call, save, _ = agent
    save("plex")
    api[1]["/status/sessions"] = (
        200,
        {
            "MediaContainer": {
                "size": 1,
                "Metadata": [
                    {
                        "title": "Example",
                        "User": {"title": "PrivateUser"},
                        "Player": {"state": "playing", "address": "192.168.1.5", "token": TOKEN},
                        "TranscodeSession": {"speed": 3, "key": "/private"},
                    }
                ],
            }
        },
        {},
    )
    reply = call("applications_query", {"profile": "app", "operation": "sessions"})
    assert reply["ok"], reply
    row = reply["result"]["data"]["items"][0]
    assert row["Player"] == {"state": "playing"}
    assert row["TranscodeSession"] == {"speed": 3}
    assert "PrivateUser" not in json.dumps(reply)
    assert api[2][-1]["headers"]["X-Plex-Token"] == TOKEN
    assert "Token" not in api[2][-1]["path"]
    api[1]["/library/sections"] = (
        200,
        '<MediaContainer size="1"><Directory key="3" title="Movies" type="movie"/></MediaContainer>',
        {},
    )
    reply = call("applications_query", {"profile": "app", "operation": "library"})
    assert reply["result"]["data"]["items"][0]["key"] == "3"


def test_plex_rescan_is_queued_mutation_using_documented_get(agent, api):
    call, save, _ = agent
    saved = save("plex")["result"]
    api[1]["/library/sections/3/refresh"] = (200, "", {})
    reply = call(
        "applications_command",
        {"profile": "app", "expected_sha256": saved["sha256"], "operation": "rescan", "ids": [3]},
        write=True,
    )
    assert reply["ok"], reply
    assert api[2][-1]["path"] == "/library/sections/3/refresh"
    assert api[2][-1]["method"] == "GET"


def test_plex_entity_document_rejected(agent, api):
    call, save, _ = agent
    save("plex")
    api[1]["/library/sections"] = (
        200,
        '<!DOCTYPE MediaContainer [<!ENTITY x SYSTEM "file:///etc/passwd">]><MediaContainer>&x;</MediaContainer>',
        {},
    )
    assert (
        call("applications_query", {"profile": "app", "operation": "library"})["error"]
        == "application invalid response"
    )


@pytest.mark.parametrize(
    "version,operation,path",
    [
        ("v5.1.0", "pause", "stop"),
        ("v5.1.0", "resume", "start"),
        ("v4.6.7", "pause", "pause"),
        ("v4.6.7", "resume", "resume"),
        ("v5.1.0", "recheck", "recheck"),
        ("v5.1.0", "reannounce", "reannounce"),
    ],
)
def test_qbittorrent_auth_versioned_commands_and_logout(agent, api, version, operation, path):
    call, save, _ = agent
    saved = save("qbittorrent")["result"]
    api[1]["/api/v2/app/version"] = (200, version, {})
    api[1][f"/api/v2/torrents/{path}"] = (200, "", {})
    reply = call(
        "applications_command",
        {
            "profile": "app",
            "expected_sha256": saved["sha256"],
            "operation": operation,
            "hashes": ["a" * 40],
        },
        write=True,
    )
    assert reply["ok"], reply
    assert parse_qs(api[2][0]["body"]) == {"username": ["admin"], "password": [PASSWORD]}
    assert api[2][2]["path"] == f"/api/v2/torrents/{path}"
    assert api[2][2]["headers"]["Cookie"] == f"SID={SID}"
    assert api[2][2]["headers"]["Origin"] == api[0]
    assert parse_qs(api[2][2]["body"])["hashes"] == ["a" * 40]
    assert api[2][-1]["path"] == "/api/v2/auth/logout"
    assert PASSWORD not in json.dumps(reply) and SID not in json.dumps(reply)


@pytest.mark.parametrize("values", [["all"], ["a" * 40 + "|all"], [], ["x"], ["a" * 40 + "\r\n"]])
def test_qbittorrent_requires_explicit_hashes_before_auth(agent, api, values):
    call, save, _ = agent
    saved = save("qbittorrent")["result"]
    reply = call(
        "applications_command",
        {
            "profile": "app",
            "expected_sha256": saved["sha256"],
            "operation": "pause",
            "hashes": values,
        },
        write=True,
    )
    assert reply["error"] == "invalid request"
    assert api[2] == []


def test_qbittorrent_failed_login_cannot_be_used(agent, api):
    call, save, _ = agent
    save("qbittorrent")
    api[1]["/api/v2/auth/login"] = (200, "Fails.", {"Set-Cookie": "SID=bogus"})
    reply = call("applications_query", {"profile": "app", "operation": "status"})
    assert reply["error"] == "application authentication failed"
    assert len(api[2]) == 1


def test_qbittorrent_downloads_are_paginated_and_secrets_omitted(agent, api):
    call, save, _ = agent
    save("qbittorrent")
    api[1]["/api/v2/torrents/info"] = (
        200,
        [
            {
                "hash": "a" * 40,
                "name": f"example {PASSWORD} {SID}",
                "state": "downloading",
                "magnet_uri": "magnet:secret",
                "tracker": "https://tracker/private",
                "save_path": "/downloads",
            }
        ],
        {},
    )
    reply = call(
        "applications_query", {"profile": "app", "operation": "downloads", "limit": 10, "page": 2}
    )
    assert reply["ok"], reply
    assert "offset=10" in api[2][1]["path"]
    assert PASSWORD not in json.dumps(reply) and SID not in json.dumps(reply)
    assert "tracker" not in json.dumps(reply) and "magnet" not in json.dumps(reply)


def test_activity_gate_fails_closed_and_counts_plex_sessions(agent, api):
    call, save, _ = agent
    save("plex")
    api[1]["/status/sessions"] = (
        200,
        {"MediaContainer": {"size": 1, "Metadata": [{"title": "Example"}]}},
        {},
    )
    reply = call("", {"profiles": ["app"]}, activity=True)["result"]
    assert reply["idle"] is False and reply["active_count"] == 1 and reply["unknown"] is False
    api[1]["/status/sessions"] = (200, {"MediaContainer": {"size": 0}}, {})
    assert call("", {"profiles": ["app"]}, activity=True)["result"]["idle"] is True
    assert call("", {"profiles": ["missing"]}, activity=True)["result"]["unknown"] is True
    api[1]["/status/sessions"] = (401, "bad", {})
    reply = call("", {"profiles": ["app"]}, activity=True)["result"]
    assert reply["idle"] is False and reply["unknown"] is True


def test_manifest_matches_routes_and_mutations_require_receipts():
    tools = json.loads((APPLICATIONS.parent / "applications-tools.json").read_text())
    assert len({tool["name"] for tool in tools}) == 8
    for tool in tools:
        assert tool["action"] in APPLICATIONS.read_text()
        assert ("request_id" in tool["required"]) is (not tool["read_only"])
        assert ("expected_sha256" in tool["required"]) is (not tool["read_only"])


def test_download_trace_matches_hash_and_does_not_read_entire_torrent_collection(agent, api):
    call, save, _ = agent
    save("sonarr", name="sonarr")
    save("qbittorrent", name="qbit")
    target = "a" * 40
    api[1]["/api/v3/queue"] = (
        200,
        {
            "totalRecords": 2,
            "records": [
                {
                    "id": 1,
                    "downloadId": target.upper(),
                    "statusMessages": [
                        {"title": "Import blocked", "messages": ["No eligible files found"]}
                    ],
                },
                {"id": 2, "downloadId": "b" * 40},
            ],
        },
        {},
    )
    api[1]["/api/v2/torrents/info"] = (
        200,
        [{"hash": target, "state": "uploading", "save_path": "/downloads"}],
        {},
    )
    reply = call(
        "applications_download_trace",
        {"arr_profile": "sonarr", "download_profile": "qbit", "download_id": target},
    )
    assert reply["ok"], reply
    result = reply["result"]
    assert result["queue_scan_complete"] is True
    assert len(result["queue_matches"]) == 1
    assert result["queue_matches"][0]["id"] == 1
    assert result["download"]["items"][0]["state"] == "uploading"
    request = next(item for item in api[2] if item["path"].startswith("/api/v2/torrents/info"))
    assert parse_qs(urlsplit(request["path"]).query)["hashes"] == [target]


@pytest.mark.parametrize(
    "status,body", [(500, "failed after commit"), (200, "invalid-json"), (200, {})]
)
def test_ambiguous_application_mutation_never_reported_as_known_failure(agent, api, status, body):
    call, save, _ = agent
    saved = save()["result"]
    api[1]["/api/v3/command"] = (status, body, {})
    reply = call(
        "applications_command",
        {"profile": "app", "expected_sha256": saved["sha256"], "operation": "rss_sync"},
        write=True,
    )
    assert reply == {"ok": False, "error": "application outcome unknown"}
    assert len(api[2]) == 1


def test_https_verifies_certificates_unless_profile_explicitly_disables(agent, tmp_path):
    call, save, _ = agent
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL CLI unavailable")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "19")
            self.end_headers()
            self.wfile.write(b'{"version":"1.0.0"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        saved = save(base_url=f"https://127.0.0.1:{server.server_port}")["result"]
        query = {"profile": "app", "operation": "status"}
        assert call("applications_query", query)["error"] == "application connection failed"
        assert save(
            base_url=f"https://127.0.0.1:{server.server_port}",
            expected_sha256=saved["sha256"],
            verify_tls=False,
        )["ok"]
        assert call("applications_query", query)["result"]["data"]["version"] == "1.0.0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_public_dispatch_registers_tools_and_rejects_arbitrary_http(agent, api):
    call, save, _ = agent
    save()
    assert call("applications_profiles", dispatch=True)["result"]["profiles"][0]["profile"] == "app"
    reply = call(
        "applications_query",
        {"profile": "app", "operation": "status", "url": "https://elsewhere.example/"},
        dispatch=True,
    )
    assert reply["error"] == "invalid request"
    reply = call(
        "applications_command",
        {
            "profile": "app",
            "expected_sha256": "a" * 64,
            "operation": "arbitrary-shell",
            "request_id": "bad-command",
        },
        dispatch=True,
    )
    assert reply["error"] == "invalid request"
    assert api[2] == []
