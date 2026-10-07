"""Real PHP/Bash tests for bounded administration, with a fake Docker boundary."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "tools/aria_bridge"
PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")
XML = "<Container><Name>app</Name><Repository>vendor/app:1</Repository></Container>"
RUNNER = r"""
require $argv[1];
require_once $argv[2];
$a = json_decode(stream_get_contents(STDIN), true);
ariaInit($a['state']);
try {
    if ($a['mode'] === 'dispatch') $r = ariaDispatch($a['state'], ['action' => $a['action'], 'arguments' => $a['arguments']]);
    elseif ($a['mode'] === 'core_execute') $r = ariaExecute($a['state'], ['job_id' => $a['job_id'], 'action' => $a['action'], 'arguments' => $a['arguments']]);
    elseif ($a['mode'] === 'read') $r = ariaOperationsRead($a['state'], $a['action'], $a['arguments']);
    elseif ($a['mode'] === 'secret') $r = ['value' => ariaOperationsSecretValue($a['state'], $a['name'])];
    else $r = ariaOperationsExecute($a['state'], ['job_id' => $a['job_id'], 'action' => $a['action'], 'arguments' => $a['arguments']]);
    echo ariaJson(['ok' => true, 'result' => $r]);
} catch (Throwable $e) { echo ariaJson(['ok' => false, 'error' => ariaError($e)]); }
"""
DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['ARIA_OPS_DOCKER_LOG']).open('a') as out:
    out.write(json.dumps(args) + '\n')
if args[0] == 'inspect':
    print(json.dumps([{'Id':'immutable-container-id','Config':{'Env':['API_KEY=container-credential']}}]))
elif args[0] == 'exec':
    print('container-credential', os.environ.get('MY_TOKEN', 'no reference'))
elif args[0] == 'logs':
    print('container-credential very-private-value')
else:
    sys.exit(1)
"""


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


@pytest.fixture
def ops(tmp_path):
    if subprocess.run(
        [PHP, "-r", "exit(function_exists('simplexml_load_string')?0:1);"], check=False
    ).returncode:
        pytest.skip("PHP SimpleXML extension is unavailable")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "app.xml").write_text(XML)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    state = tmp_path / "state"
    docker = tmp_path / "docker"
    docker.write_text(DOCKER)
    docker.chmod(0o700)
    log = tmp_path / "docker.jsonl"
    environment = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_AGENT_FILE_ROOTS": str(tmp_path),
        "ARIA_AGENT_TEMPLATES": str(templates),
        "ARIA_AGENT_SCRIPTS": str(scripts),
        "ARIA_AGENT_DOCKER": str(docker),
        "ARIA_OPS_DOCKER_LOG": str(log),
    }
    counter = 0

    def call(action, arguments=None, read=False, raw=False, core=False, execute=False):
        nonlocal counter
        counter += 1
        result = subprocess.run(
            [PHP, "-r", RUNNER, str(BASE / "host-agent.php"), str(BASE / "host-operations.php")],
            input=json.dumps(
                {
                    "state": str(state),
                    "mode": ("core_execute" if execute else "dispatch")
                    if core
                    else ("read" if read else "execute"),
                    "job_id": f"operation-{counter}",
                    "action": action if core else f"operations_{action}",
                    "arguments": arguments or {},
                }
            ),
            text=True,
            capture_output=True,
            env=environment,
            check=True,
            timeout=20,
        )
        assert result.stderr == "", result.stderr
        parsed = json.loads(result.stdout)
        if raw:
            return parsed
        assert parsed["ok"], parsed
        return parsed["result"]

    def secret(value="very-private-value", name="test-token", revision=""):
        source = tmp_path / "credential-input"
        source.write_text(value)
        return call(
            "secret_import",
            {
                "name": name,
                "source_path": str(source),
                "expected_source_sha256": sha(value),
                "expected_revision": revision,
            },
        )

    return {
        "call": call,
        "secret": secret,
        "path": tmp_path,
        "state": state,
        "scripts": scripts,
        "templates": templates,
        "log": log,
    }


def test_manifest_has_unique_actions_and_request_ids():
    rows = json.loads((BASE / "operations-tools.json").read_text())
    assert len(rows) >= 20
    assert len({row["name"] for row in rows}) == len(rows)
    assert len({row["action"] for row in rows}) == len(rows)
    for row in rows:
        assert row["action"].startswith("operations_")
        assert ("request_id" in row["required"]) != row["read_only"]
        assert set(row["required"]).issubset(row["properties"])


def test_host_exec_does_not_interpret_shell_metacharacters(ops):
    marker = ops["path"] / "should-not-exist"
    literal = f"$(touch {marker}); echo unsafe"
    result = ops["call"]("host_exec", {"argv": ["/usr/bin/printf", "%s", literal]})
    assert result["exit_code"] == 0
    assert result["output"] == literal
    assert not marker.exists()


@pytest.mark.parametrize("argv", [[], ["printf", "hello"], ["/bin/echo", "nul\0value"]])
def test_invalid_execution_arguments_rejected(ops, argv):
    assert ops["call"]("host_exec", {"argv": argv}, raw=True)["error"] == "invalid request"


def test_execution_timeout_and_capture_are_bounded(ops):
    result = ops["call"]("host_exec", {"argv": ["/bin/sleep", "4"], "timeout_seconds": 1})
    assert result["timed_out"]
    assert result["exit_code"] != 0


def test_secret_import_is_reference_only_cas_and_restrictive(ops):
    first = ops["secret"]()
    assert "value" not in first
    listed = ops["call"]("secret_list", read=True)
    assert listed["secrets"][0]["revision"] == first["revision"]
    assert "very-private-value" not in json.dumps(listed)
    secret_path = ops["state"] / "secrets/test-token.json"
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert secret_path.parent.stat().st_mode & 0o777 == 0o700
    old = secret_path.read_text()
    with pytest.raises(AssertionError, match="hash mismatch"):
        ops["secret"]("replacement")
    assert secret_path.read_text() == old
    second = ops["secret"]("replacement", revision=first["revision"])
    assert first["revision"] != second["revision"]
    bad = ops["call"](
        "secret_delete", {"name": "test-token", "expected_revision": first["revision"]}, raw=True
    )
    assert bad["error"] == "hash mismatch"
    assert ops["call"](
        "secret_delete", {"name": "test-token", "expected_revision": second["revision"]}
    )["deleted"]


def test_secret_source_hash_does_not_import_changed_content(ops):
    path = ops["path"] / "input"
    path.write_text("secret")
    result = ops["call"](
        "secret_import",
        {
            "name": "key",
            "source_path": str(path),
            "expected_source_sha256": "0" * 64,
            "expected_revision": "",
        },
        raw=True,
    )
    assert result["error"] == "hash mismatch"
    assert not (ops["state"] / "secrets/key.json").exists()


def test_secret_json_components_are_redacted_from_command_output(ops):
    ops["secret"]('{"username":"some-account","password":"opaque-password"}')
    result = ops["call"]("host_exec", {"argv": ["/bin/echo", "some-account opaque-password"]})
    assert result["output"] == "[REDACTED] [REDACTED]\n"


def test_script_create_syntax_update_backup_and_literal_arguments(ops):
    script = '#!/bin/bash\nprintf "%s|%s" "$1" "$MY_TOKEN"\n'
    initial = ops["call"](
        "script_save", {"name": "Test script", "script": script, "expected_sha256": ""}
    )
    assert initial["syntax_valid"]
    assert initial["sha256"] == sha(script)
    ops["secret"]()
    ran = ops["call"](
        "script_run_arguments",
        {
            "name": "Test script",
            "expected_sha256": initial["sha256"],
            "arguments": ["$(exit 4); spaces survive"],
            "secret_environment": [{"environment": "MY_TOKEN", "secret_ref": "test-token"}],
        },
    )
    assert ran["exit_code"] == 0
    assert ran["output"] == "$(exit 4); spaces survive|[REDACTED]"
    assert not list((ops["state"] / "jobs").glob("*.script"))
    second = ops["call"](
        "script_save",
        {
            "name": "Test script",
            "script": "#!/bin/bash\necho new\n",
            "expected_sha256": sha(script),
        },
    )
    backup = json.loads((ops["state"] / f"file-backups/{second['backup_id']}.json").read_text())
    assert backup["sha256"] == sha(script)


def test_script_invalid_syntax_does_not_replace_existing_script(ops):
    script = "#!/bin/bash\necho original\n"
    ops["call"]("script_save", {"name": "Existing", "script": script, "expected_sha256": ""})
    bad = ops["call"](
        "script_save",
        {"name": "Existing", "script": "if broken", "expected_sha256": sha(script)},
        raw=True,
    )
    assert bad["error"] == "invalid request"
    assert (ops["scripts"] / "Existing/script").read_text() == script
    assert not list((ops["state"] / "jobs").glob("*.syntax"))


def test_script_run_rejects_unreviewed_bytes(ops):
    ops["call"]("script_save", {"name": "Changed", "script": "echo changed", "expected_sha256": ""})
    result = ops["call"](
        "script_run_arguments", {"name": "Changed", "expected_sha256": "0" * 64}, raw=True
    )
    assert result["error"] == "hash mismatch"


@pytest.mark.parametrize("environment", ["BASH_ENV", "PATH", "LD_PRELOAD", "NODE_OPTIONS"])
def test_credential_environment_cannot_reconfigure_interpreters(ops, environment):
    ops["secret"]()
    result = ops["call"](
        "host_exec",
        {
            "argv": ["/bin/echo", "ok"],
            "secret_environment": [{"environment": environment, "secret_ref": "test-token"}],
        },
        raw=True,
    )
    assert result["error"] == "invalid request"


def test_container_exec_pins_id_uses_env_reference_and_redacts(ops):
    ops["secret"]()
    result = ops["call"](
        "container_exec",
        {
            "name": "app",
            "argv": ["/bin/echo", "one argument"],
            "user": "1000:1000",
            "working_directory": "/data",
            "secret_environment": [{"environment": "MY_TOKEN", "secret_ref": "test-token"}],
        },
    )
    assert result["output"] == "[REDACTED] [REDACTED]\n"
    calls = [json.loads(row) for row in ops["log"].read_text().splitlines()]
    assert calls[-1] == [
        "exec",
        "--user",
        "1000:1000",
        "--workdir",
        "/data",
        "--env",
        "MY_TOKEN",
        "immutable-container-id",
        "/bin/echo",
        "one argument",
    ]
    assert "very-private-value" not in ops["log"].read_text()


def test_container_exec_protects_bridge(ops):
    result = ops["call"](
        "container_exec", {"name": "aria-gpt-bridge", "argv": ["/bin/true"]}, raw=True
    )
    assert result["error"] == "protected container"
    assert not ops["log"].exists()


def test_file_create_replace_and_restore_preserve_modes_and_owners(ops):
    path = ops["path"] / "configuration"
    ops["call"]("file_write", {"path": str(path), "content": "old", "expected_sha256": ""})
    path.chmod(0o640)
    before = path.stat()
    result = ops["call"](
        "file_write", {"path": str(path), "content": "new", "expected_sha256": sha("old")}
    )
    assert path.read_text() == "new"
    assert path.stat().st_mode & 0o777 == 0o640
    assert (path.stat().st_uid, path.stat().st_gid) == (before.st_uid, before.st_gid)
    ops["call"]("file_restore", {"backup_id": result["backup_id"], "expected_sha256": sha("new")})
    assert path.read_text() == "old"


def test_file_cas_and_redaction_placeholders_do_not_overwrite(ops):
    path = ops["path"] / "configuration"
    path.write_text("original")
    for content, expected in [("other", ""), ("other", "0" * 64), ("[REDACTED]", sha("original"))]:
        result = ops["call"](
            "file_write",
            {"path": str(path), "content": content, "expected_sha256": expected},
            raw=True,
        )
        assert not result["ok"]
        assert path.read_text() == "original"


@pytest.mark.parametrize("suffix", ["/../outside", "//other", "/state/jobs", "/shadow"])
def test_file_read_rejects_traversal_state_and_shadow(ops, suffix):
    result = ops["call"]("file_read", {"path": str(ops["path"]) + suffix}, read=True, raw=True)
    assert not result["ok"]


def test_file_operations_reject_symlink_components(ops):
    actual = ops["path"] / "real"
    actual.mkdir()
    (actual / "data").write_text("data")
    link = ops["path"] / "linked"
    link.symlink_to(actual, target_is_directory=True)
    result = ops["call"]("file_read", {"path": str(link / "data")}, read=True, raw=True)
    assert result["error"] == "invalid request"
    assert not ops["call"](
        "file_write",
        {"path": str(link / "new"), "content": "data", "expected_sha256": ""},
        raw=True,
    )["ok"]
    assert not (actual / "new").exists()


def test_file_read_is_bounded_and_scrubs_known_credentials(ops):
    ops["secret"]()
    path = ops["path"] / "log"
    text = "prefix\n" * 10000 + "very-private-value"
    path.write_text(text)
    result = ops["call"]("file_read", {"path": str(path)}, read=True)
    assert result["sha256"] == sha(text)
    assert result["truncated"]
    assert len(result["content"]) <= 65536
    assert "very-private-value" not in result["content"]
    assert "[REDACTED]" in result["content"]
    path.write_text("x" * 131073)
    assert not ops["call"]("file_read", {"path": str(path)}, read=True, raw=True)["ok"]


def test_directory_pagination_and_compare_before_permission_change(ops):
    directory = ops["path"] / "pages"
    directory.mkdir()
    for i in range(3):
        (directory / str(i)).write_text(str(i))
    page = ops["call"]("file_list", {"path": str(directory), "limit": 2}, read=True)
    assert len(page["entries"]) == 2
    assert page["next_offset"] == 2
    assert (
        len(
            ops["call"]("file_list", {"path": str(directory), "offset": 2, "limit": 2}, read=True)[
                "entries"
            ]
        )
        == 1
    )
    stat = ops["call"]("file_stat", {"path": str(directory)}, read=True)["stat"]
    changed = ops["call"](
        "file_permissions",
        {"path": str(directory), "mode": "0700", "expected_signature": stat["signature"]},
    )
    assert changed["after"]["mode"] == "0700"
    assert not ops["call"](
        "file_permissions",
        {"path": str(directory), "mode": "0755", "expected_signature": stat["signature"]},
        raw=True,
    )["ok"]


def test_archive_create_verify_restore_and_pre_restore_backup(ops):
    script = "#!/bin/bash\necho saved\n"
    ops["call"]("script_save", {"name": "Saved", "script": script, "expected_sha256": ""})
    archive = ops["call"]("archive_create")
    assert archive["entry_count"] == 2
    verified = ops["call"](
        "archive_verify",
        {"archive_id": archive["archive_id"], "expected_sha256": archive["sha256"]},
    )
    assert verified["verified"]
    (ops["templates"] / "app.xml").write_text(XML.replace(":1", ":2"))
    (ops["scripts"] / "Saved/script").unlink()
    inventory = ops["call"]("config_inventory")
    result = ops["call"](
        "archive_restore",
        {
            "archive_id": archive["archive_id"],
            "expected_sha256": archive["sha256"],
            "expected_inventory_sha256": inventory["inventory_sha256"],
            "overwrite": True,
        },
    )
    assert result["restored_entries"] == 2
    assert result["previous_archive"]["archive_id"] != archive["archive_id"]
    assert (ops["templates"] / "app.xml").read_text() == XML
    assert (ops["scripts"] / "Saved/script").read_text() == script
    assert not result["deployed_containers"]


def test_archive_restore_rejects_changed_inventory_before_writing(ops):
    archive = ops["call"]("archive_create")
    inventory = ops["call"]("config_inventory")
    altered = XML.replace(":1", ":9")
    (ops["templates"] / "app.xml").write_text(altered)
    result = ops["call"](
        "archive_restore",
        {
            "archive_id": archive["archive_id"],
            "expected_sha256": archive["sha256"],
            "expected_inventory_sha256": inventory["inventory_sha256"],
            "overwrite": True,
        },
        raw=True,
    )
    assert result["error"] == "hash mismatch"
    assert (ops["templates"] / "app.xml").read_text() == altered


@pytest.mark.parametrize("corruption", ["hash", "traversal", "duplicate", "gzip"])
def test_archive_verification_rejects_tampering_and_traversal(ops, corruption):
    archive = ops["call"]("archive_create")
    path = ops["state"] / f"config-archives/{archive['archive_id']}.json.gz"
    data = json.loads(gzip.decompress(path.read_bytes()))
    if corruption == "hash":
        data["entries"][0]["sha256"] = "0" * 64
    elif corruption == "traversal":
        data["entries"][0]["name"] = "../escape.xml"
    elif corruption == "duplicate":
        data["entries"].append(data["entries"][0])
    raw = b"bad gzip" if corruption == "gzip" else gzip.compress(json.dumps(data).encode())
    path.write_bytes(raw)
    result = ops["call"](
        "archive_verify",
        {"archive_id": archive["archive_id"], "expected_sha256": sha(raw)},
        raw=True,
    )
    assert result["error"] == "invalid request"


def test_archive_skips_unchanged_bridge_template(ops):
    (ops["templates"] / "bridge.xml").write_text(XML.replace("<Name>app", "<Name>aria-gpt-bridge"))
    archive = ops["call"]("archive_create")
    inventory = ops["call"]("config_inventory")
    result = ops["call"](
        "archive_restore",
        {
            "archive_id": archive["archive_id"],
            "expected_sha256": archive["sha256"],
            "expected_inventory_sha256": inventory["inventory_sha256"],
        },
    )
    assert result["restored_entries"] == 0


def test_archive_metadata_never_contains_script_contents(ops):
    ops["call"](
        "script_save",
        {"name": "Private", "script": "echo embedded-private-string", "expected_sha256": ""},
    )
    ops["call"]("archive_create")
    listed = ops["call"]("archive_list", read=True)
    assert "embedded-private-string" not in json.dumps(listed)
    for path in (ops["state"] / "config-archives").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_core_script_read_and_run_scrub_vault_and_preserve_read_limit(ops):
    ops["secret"]()
    script = "#!/bin/bash\n" + "# safe comment\n" * 6000 + "printf 'very-private-value'\n"
    ops["call"]("script_save", {"name": "Legacy", "script": script, "expected_sha256": ""})
    read = ops["call"]("script_get", {"name": "Legacy"}, core=True)
    assert "very-private-value" not in read["script"]
    assert read["script"].startswith("#!/bin/bash\n")
    assert len(read["script"]) > 65536
    run = ops["call"](
        "script_run",
        {"name": "Legacy", "expected_sha256": sha(script), "timeout_seconds": 5},
        core=True,
        execute=True,
    )
    assert run["output"] == "[REDACTED]"
    logs = ops["call"]("container_logs", {"name": "app", "tail": 10}, core=True)
    assert "very-private-value" not in logs["output"]
    assert "container-credential" not in logs["output"]


def test_nested_object_key_order_deduplicates_but_array_order_is_preserved(ops):
    arguments = {
        "argv": ["/bin/echo", "one", "two"],
        "secret_environment": [{"environment": "MY_TOKEN", "secret_ref": "test-token"}],
        "request_id": "nested-order-test",
    }
    first = ops["call"]("operations_host_exec", arguments, core=True)
    arguments["secret_environment"] = [{"secret_ref": "test-token", "environment": "MY_TOKEN"}]
    second = ops["call"]("operations_host_exec", arguments, core=True)
    assert first["job_id"] == second["job_id"]
    assert second["deduplicated"]
    arguments["argv"] = ["/bin/echo", "two", "one"]
    changed = ops["call"]("operations_host_exec", arguments, core=True, raw=True)
    assert changed["error"] == "request_id conflict"


def test_dashboard_token_is_redacted_without_a_vault_directory(ops):
    ops["call"]("file_roots", read=True)
    token = "a1" * 32
    (ops["state"] / "dashboard-token").write_text(token + "\n")
    assert not (ops["state"] / "secrets").exists()
    result = ops["call"]("host_exec", {"argv": ["/bin/echo", token]})
    assert result["output"] == "[REDACTED]\n"
    script = "#!/bin/bash\nprintf '%s' '" + token + "'\n"
    ops["call"]("script_save", {"name": "Credential echo", "script": script, "expected_sha256": ""})
    result = ops["call"](
        "script_run",
        {"name": "Credential echo", "expected_sha256": sha(script), "timeout_seconds": 5},
        core=True,
        execute=True,
    )
    assert result["output"] == "[REDACTED]"
    read = ops["call"]("script_get", {"name": "Credential echo"}, core=True)
    assert token not in read["script"]
