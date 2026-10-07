"""Real local Git fast-forwards with isolated Docker/deployment boundaries."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "tools/aria_bridge"
ORIGIN = "https://github.com/kevinwaynekelly/kevin-cogs.git"
OLD_ID = "a" * 64
NEW_ID = "c" * 64
JOB_ID = "self_update_regression"


GIT_WRAPPER = r"""
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["ARIA_TEST_GIT_TRACE"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if "fetch" in args:
    args[args.index("origin", args.index("fetch"))] = os.environ["ARIA_TEST_REMOTE"]
sys.exit(subprocess.call([os.environ["ARIA_TEST_REAL_GIT"]] + args))
"""


DOCKER_FAKE = r"""
import json, os, sys
from pathlib import Path
path = Path(os.environ["ARIA_TEST_DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state["calls"].append(args)
result = None
rc = 0
def find(value):
    return next((c for c in state["containers"] if c["id"] == value or c["name"] == "/" + value), None)
if args[0] == "inspect":
    result = find(args[-1])
    if result is None: rc = 1
elif args[0] == "rm":
    found = find(args[-1])
    if found: state["containers"].remove(found)
elif args[0] == "rename":
    found = find(args[1])
    if not found or find(args[2]): rc = 1
    else: found["name"] = "/" + args[2]
elif args[0] == "start":
    found = find(args[-1])
    if found: found.update(running=True, status="running", health="healthy")
    else: rc = 1
elif args[0] == "stop":
    found = find(args[-1])
    if found: found.update(running=False, status="exited")
    else: rc = 1
else:
    rc = 41
path.write_text(json.dumps(state))
if result is not None: print(json.dumps(result))
sys.exit(rc)
"""


DEPLOYMENT_FAKE = r"""
import json, os, subprocess, sys
from pathlib import Path
action = sys.argv[1:]
with Path(os.environ["ARIA_TEST_DEPLOY_TRACE"]).open("a") as stream:
    stream.write(json.dumps(action) + "\n")
if action[0] != "upgrade":
    sys.exit(0)
mode = os.environ.get("ARIA_TEST_UPGRADE_MODE", "success")
source = Path(os.environ["ARIA_BRIDGE_UPDATE_SOURCE"])
git = os.environ["ARIA_TEST_REAL_GIT"]
if mode in {"dirty", "moved"}:
    (source / "version.txt").write_text("concurrent user edit\n")
    if mode == "moved":
        subprocess.run([git, "-C", str(source), "add", "version.txt"], check=True)
        subprocess.run([git, "-C", str(source), "commit", "-qm", "concurrent user commit"], check=True)
    print("runtime-secret-never-publish-this-output")
    sys.exit(17)
path = Path(os.environ["ARIA_TEST_DOCKER_STATE"])
state = json.loads(path.read_text())
old = state["containers"][0]
new = dict(old, id="c" * 64, image="sha256:" + "d" * 64,
           update_id=os.environ["ARIA_BRIDGE_UPDATE_ID"], health="healthy")
if mode in {"partial", "foreign"}:
    old.update(name="/aria-gpt-bridge-rollback-123456-789", running=False, status="exited")
    new["health"] = "unhealthy"
    if mode == "foreign": new["update_id"] = "another-administrator"
    state["containers"].append(new)
    rc = 124
elif mode == "fail":
    rc = 17
else:
    state["containers"] = [new]
    if mode == "unhealthy": new["health"] = "unhealthy"
    rc = 0
path.write_text(json.dumps(state))
if mode == "verbose": print("build output " * 20000)
print("runtime-secret-never-publish-this-output")
sys.exit(rc)
"""


@pytest.fixture
def updater(tmp_path):
    php = shutil.which("php")
    git = shutil.which("git")
    if not php or not git:
        pytest.skip("Bridge updater requires native PHP and Git")

    def git_run(*args, cwd=None):
        return subprocess.run(
            [git, *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    upstream = tmp_path / "upstream"
    source = tmp_path / "source"
    git_run("init", "--bare", "--initial-branch=main", str(remote))
    git_run("clone", str(remote), str(upstream))
    git_run("config", "user.name", "Aria Tests", cwd=upstream)
    git_run("config", "user.email", "aria@example.invalid", cwd=upstream)
    (upstream / "version.txt").write_text("initial version\n")
    git_run("add", "version.txt", cwd=upstream)
    git_run("commit", "-m", "initial", cwd=upstream)
    git_run("push", "origin", "main", cwd=upstream)
    git_run("clone", str(remote), str(source))
    git_run("remote", "set-url", "origin", ORIGIN, cwd=source)
    git_run("config", "user.name", "Aria Tests", cwd=source)
    git_run("config", "user.email", "aria@example.invalid", cwd=source)
    before = git_run("rev-parse", "HEAD", cwd=source)
    (upstream / "version.txt").write_text("updated version\n")
    git_run("commit", "-am", "update", cwd=upstream)
    git_run("push", "origin", "main", cwd=upstream)
    target = git_run("rev-parse", "HEAD", cwd=upstream)
    state = tmp_path / "appdata/management"
    (state / "bridge-updates").mkdir(parents=True)
    record = state / f"bridge-updates/{JOB_ID}.json"
    record.write_text(
        json.dumps(
            {
                "job_id": JOB_ID,
                "request_id": "client_request",
                "action": "bridge_update",
                "status": "queued",
                "phase": "queued",
                "created_at": "2026-10-07T00:00:00Z",
            }
        )
    )
    docker_state = tmp_path / "docker.json"
    docker_state.write_text(
        json.dumps(
            {
                "calls": [],
                "containers": [
                    {
                        "id": OLD_ID,
                        "name": "/aria-gpt-bridge",
                        "image": "sha256:" + "b" * 64,
                        "running": True,
                        "status": "running",
                        "health": "healthy",
                        "update_id": None,
                    }
                ],
            }
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, code in (("git-wrapper", GIT_WRAPPER), ("docker", DOCKER_FAKE)):
        path = binaries / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)
    helper = binaries / "deployment-helper.py"
    helper.write_text(DEPLOYMENT_FAKE)
    for name, action in (("upgrade.sh", "upgrade"), ("service.sh", "service")):
        # File paths are fixed fixture arguments and quoted as shell argv.
        (binaries / name).write_text(
            '#!/bin/bash\nexec "$ARIA_TEST_PYTHON" "$ARIA_TEST_HELPER" ' + action + ' "$@"\n'
        )
    trace = tmp_path / "deploy-trace.jsonl"
    git_trace = tmp_path / "git-trace.jsonl"
    env = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_BRIDGE_UPDATE_SOURCE": str(source),
        "ARIA_BRIDGE_UPDATE_GIT": str(binaries / "git-wrapper"),
        "ARIA_BRIDGE_UPDATE_DOCKER": str(binaries / "docker"),
        "ARIA_BRIDGE_UPDATE_UPGRADE": str(binaries / "upgrade.sh"),
        "ARIA_BRIDGE_UPDATE_SERVICE": str(binaries / "service.sh"),
        "ARIA_TEST_REAL_GIT": git,
        "ARIA_TEST_REMOTE": str(remote),
        "ARIA_TEST_GIT_TRACE": str(git_trace),
        "ARIA_TEST_DOCKER_STATE": str(docker_state),
        "ARIA_TEST_DEPLOY_TRACE": str(trace),
        "ARIA_TEST_PYTHON": sys.executable,
        "ARIA_TEST_HELPER": str(helper),
    }

    def run(mode="success"):
        completed = subprocess.run(
            [php, str(BRIDGE / "bridge-update.php"), str(state), JOB_ID],
            env={**env, "ARIA_TEST_UPGRADE_MODE": mode},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        job = json.loads(record.read_text())
        assert "runtime-secret-never-publish-this-output" not in (
            record.read_text() + completed.stdout + completed.stderr
        )
        return completed, job

    return SimpleNamespace(
        run=run,
        git=git_run,
        source=source,
        before=before,
        target=target,
        record=record,
        docker_state=docker_state,
        trace=trace,
        git_trace=git_trace,
    )


@pytest.mark.parametrize("mode", ["success", "verbose"])
def test_bridge_update_fast_forwards_exact_target_and_verifies_health(updater, mode):
    completed, job = updater.run(mode)
    assert completed.returncode == 0, job
    assert job["status"] == "succeeded"
    assert job["from_revision"] == updater.before
    assert job["to_revision"] == updater.target
    assert updater.git("rev-parse", "HEAD", cwd=updater.source) == updater.target
    assert job["result"]["bridge_healthy"] is True
    calls = [json.loads(line) for line in updater.git_trace.read_text().splitlines()]
    merge = next(call for call in calls if "merge" in call)
    assert merge[-1] == updater.target
    assert "--ff-only" in merge
    assert not any("--hard" in call for call in calls)


def test_bridge_update_rebuilds_when_checkout_is_already_latest(updater):
    updater.git("fetch", str(updater.source.parent / "remote.git"), "main", cwd=updater.source)
    updater.git("merge", "--ff-only", updater.target, cwd=updater.source)
    completed, job = updater.run()
    assert completed.returncode == 0, job
    assert job["from_revision"] == job["to_revision"] == updater.target
    assert ["upgrade"] in [json.loads(line) for line in updater.trace.read_text().splitlines()]


@pytest.mark.parametrize("condition", ["dirty", "wrong_origin", "wrong_branch", "diverged"])
def test_bridge_update_rejects_unsafe_source_before_deployment(updater, condition):
    if condition == "dirty":
        (updater.source / "version.txt").write_text("my local edit\n")
    elif condition == "wrong_origin":
        updater.git(
            "remote",
            "set-url",
            "origin",
            "https://github.com/other/repository.git",
            cwd=updater.source,
        )
    elif condition == "wrong_branch":
        updater.git("checkout", "-b", "feature", cwd=updater.source)
    else:
        (updater.source / "local.txt").write_text("local commit\n")
        updater.git("add", "local.txt", cwd=updater.source)
        updater.git("commit", "-m", "local", cwd=updater.source)
    original = updater.git("rev-parse", "HEAD", cwd=updater.source)
    completed, job = updater.run()
    assert completed.returncode != 0
    assert job["status"] == "failed"
    assert updater.git("rev-parse", "HEAD", cwd=updater.source) == original
    assert not updater.trace.exists()


@pytest.mark.parametrize("mode", ["fail", "partial"])
def test_bridge_failure_restores_source_host_and_original_container(updater, mode):
    completed, job = updater.run(mode)
    assert completed.returncode != 0
    assert job["status"] == "failed", job
    assert job["result"]["rollback"]["status"] == "complete"
    assert updater.git("rev-parse", "HEAD", cwd=updater.source) == updater.before
    state = json.loads(updater.docker_state.read_text())
    assert len(state["containers"]) == 1
    assert state["containers"][0]["id"] == OLD_ID
    assert state["containers"][0]["name"] == "/aria-gpt-bridge"
    assert state["containers"][0]["health"] == "healthy"
    calls = [json.loads(line) for line in updater.trace.read_text().splitlines()]
    assert ["service", "restart"] in calls
    assert ["service", "install-boot"] in calls
    if mode == "partial":
        assert ["rm", "-f", NEW_ID] in state["calls"]
        assert any(item["timed_out"] for item in job["diagnostics"])


@pytest.mark.parametrize("mode", ["dirty", "moved"])
def test_failed_update_never_discards_concurrent_source_changes(updater, mode):
    completed, job = updater.run(mode)
    assert completed.returncode != 0
    assert job["status"] == "unknown", job
    assert job["result"]["rollback"]["status"] == "incomplete"
    assert (updater.source / "version.txt").read_text() == "concurrent user edit\n"
    assert updater.git("rev-parse", "HEAD", cwd=updater.source) != updater.before
    calls = [json.loads(line) for line in updater.git_trace.read_text().splitlines()]
    assert not any("reset" in call for call in calls)


def test_failed_update_preserves_container_created_by_another_actor(updater):
    completed, job = updater.run("foreign")
    assert completed.returncode != 0
    assert job["status"] == "unknown"
    state = json.loads(updater.docker_state.read_text())
    assert len(state["containers"]) == 2
    assert not any(call[0] == "rm" for call in state["calls"])


def test_final_health_is_checked_even_if_upgrade_script_returns_success(updater):
    completed, job = updater.run("unhealthy")
    assert completed.returncode != 0
    assert job["status"] == "unknown"
    assert job["error"] == "bridge final health verification failed"
    assert updater.git("rev-parse", "HEAD", cwd=updater.source) == updater.target


def test_unknown_job_is_never_replayed(updater):
    record = json.loads(updater.record.read_text())
    record["status"] = "unknown"
    updater.record.write_text(json.dumps(record))
    completed, after = updater.run()
    assert completed.returncode != 0
    assert after == record
    assert not updater.git_trace.exists()
    assert not updater.trace.exists()


def test_updater_refuses_active_manual_installer_before_fetch(updater):
    install_lock = updater.record.parents[2] / "install.lock"
    with install_lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed, job = updater.run()
    assert completed.returncode != 0
    assert job["error"] == "another bridge installer is running"
    assert not updater.git_trace.exists()
    assert not updater.trace.exists()


@pytest.mark.parametrize("internal_id", [None, "wrong_job", "stale_job", JOB_ID])
def test_installer_interlock_only_admits_running_update_child(tmp_path, internal_id):
    if not shutil.which("jq"):
        pytest.skip("Installer interlock requires jq")
    data = tmp_path / "appdata"
    state = data / "management"
    (state / "bridge-updates").mkdir(parents=True)
    (state / f"bridge-updates/{JOB_ID}.json").write_text(
        json.dumps({"job_id": JOB_ID, "action": "bridge_update", "status": "running"})
    )
    (state / "bridge-update-latest.json").write_text(json.dumps({"job_id": JOB_ID}))
    (state / "bridge-updates/stale_job.json").write_text(
        json.dumps({"job_id": "stale_job", "action": "bridge_update", "status": "running"})
    )
    env = {**os.environ}
    env.pop("ARIA_BRIDGE_UPDATE_ID", None)
    if internal_id is not None:
        env["ARIA_BRIDGE_UPDATE_ID"] = internal_id
    with (state / "bridge-update-runner.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; aria_data="$2"; aria_install_lock',
                "installer-test",
                str(BRIDGE / "unraid-common.sh"),
                str(data),
            ],
            env=env,
            capture_output=True,
            timeout=5,
            check=False,
        )
    assert (result.returncode == 0) == (internal_id == JOB_ID), result.stderr


def test_replacement_supervisor_survives_updater_process_group_exit(tmp_path):
    if not shutil.which("setsid"):
        pytest.skip("Host launcher requires setsid")
    # Execute the actual launch statement against a harmless replacement script.
    line = next(
        line.strip()
        for line in (BRIDGE / "host-service.sh").read_text().splitlines()
        if 'nohup bash "$aria_source/host-service.sh" _supervise' in line
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "host-service.sh").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$$" > "$ARIA_APPDATA_ROOT/supervisor.pid"\n'
        "exec /bin/sleep 30\n"
    )
    launcher_script = 'aria_data="$1"\naria_source="$2"\naria_role=work\n' + line + "\nwait\n"
    launcher = subprocess.Popen(
        ["bash", "-c", launcher_script, "updater", str(tmp_path), str(source)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    supervisor_pid = None
    try:
        record = tmp_path / "supervisor.pid"
        for _ in range(250):
            if record.exists() and record.read_text().strip():
                break
            time.sleep(0.01)
        assert record.exists()
        supervisor_pid = int(record.read_text())
        assert os.getpgid(supervisor_pid) != launcher.pid
        os.killpg(launcher.pid, signal.SIGTERM)
        launcher.wait(timeout=3)
        os.kill(supervisor_pid, 0)
    finally:
        if launcher.poll() is None:
            os.killpg(launcher.pid, signal.SIGKILL)
            launcher.wait(timeout=3)
        if supervisor_pid is not None:
            try:
                os.kill(supervisor_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
