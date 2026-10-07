"""Run repository migration against local Git and isolated host boundaries."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "tools/aria_bridge"
OLD_ORIGIN = "https://github.com/kevinwaynekelly/kevin-cogs.git"
NEW_ORIGIN = "https://github.com/kevinwaynekelly/aria-gpt-bridge.git"

GIT_WRAPPER = r"""
import os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if "clone" in args:
    if os.environ.get("ARIA_TEST_CLONE_FAIL"): sys.exit(37)
    expected = "https://github.com/kevinwaynekelly/aria-gpt-bridge.git"
    assert args[-2] == expected
    args[-2] = os.environ["ARIA_TEST_REMOTE"]
    subprocess.run([os.environ["ARIA_TEST_REAL_GIT"], *args], check=True)
    subprocess.run([os.environ["ARIA_TEST_REAL_GIT"], "-C", args[-1], "remote", "set-url", "origin", expected], check=True)
    if os.environ.get("ARIA_TEST_TARGET_RACE"):
        target = Path(os.environ["ARIA_APPDATA_ROOT"]) / "source-standalone"
        target.mkdir()
        (target / "keep.txt").write_text("concurrent directory")
    sys.exit(0)
os.execv(os.environ["ARIA_TEST_REAL_GIT"], ["git", *args])
"""

DOCKER_FAKE = r"""
import fcntl, json, os, sys
from pathlib import Path
path = Path(os.environ["ARIA_TEST_DOCKER"])
data = json.loads(path.read_text())
args = sys.argv[1:]
data["calls"].append(args)
rc = 0
result = ""
if args[0] == "inspect" and "--format" not in args:
    result = json.dumps([data["inspect"]])
elif args[0] == "inspect":
    if args[args.index("--format") + 1].startswith("{{.Id}}"):
        result = data["containers"][args[-1]]["id"] + " <no value>"
    else:
        result = "running healthy"
elif args[:2] == ["container", "inspect"]:
    rc = 0 if args[-1] in data["containers"] else 1
elif args[0] == "build":
    data["locks_held"] = {}
    for name in ("install.lock", "management/queue.lock"):
        with (Path(os.environ["ARIA_APPDATA_ROOT"]) / name).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data["locks_held"][name] = False
            except BlockingIOError:
                data["locks_held"][name] = True
    rc = 13 if os.environ.get("ARIA_TEST_BUILD_FAIL") else 0
elif args[0] == "stop":
    data["containers"][args[-1]]["running"] = False
elif args[0] == "rename":
    data["containers"][args[2]] = data["containers"].pop(args[1])
elif args[0] == "run":
    if os.environ.get("ARIA_TEST_RUN_FAIL"):
        rc = 17
    else:
        data["containers"]["aria-gpt-bridge"] = {"id": "b" * 64, "running": True}
        result = "b" * 64
elif args[0] == "start":
    data["containers"][args[-1]]["running"] = True
elif args[0] == "rm":
    data["containers"].pop(args[-1], None)
elif args[0] != "info":
    rc = 41
path.write_text(json.dumps(data))
if result: print(result)
sys.exit(rc)
"""


@pytest.fixture
def migration(tmp_path):
    if not all(shutil.which(command) for command in ("git", "php", "jq", "flock")):
        pytest.skip("Migration requires native Git, PHP, jq and flock")
    git = shutil.which("git")

    def git_run(*args, cwd=None):
        return subprocess.run(
            [git, *args], cwd=cwd, check=True, text=True, capture_output=True
        ).stdout.strip()

    data = tmp_path / "persistent appdata"
    data.mkdir()
    state = data / "management"
    for name in ("jobs", "bridge-updates", "requests"):
        (state / name).mkdir(parents=True)
    (data / "status").mkdir()
    (data / "runtime-key").write_text("private-runtime-token")
    (data / "red-token").write_text("private-red-token")
    (state / "settings.json").write_text('{"keep": "settings"}')
    (state / "requests/existing.json").write_text('{"keep": "receipt"}')
    (data / "status/containers.json").write_text('{"keep": "snapshot"}')
    old = data / "source"
    upstream = tmp_path / "new upstream"
    for source, version in ((old, "old"), (upstream, "new")):
        bridge = source / "tools/aria_bridge"
        bridge.mkdir(parents=True)
        for name in ("unraid-common.sh", "upgrade-unraid.sh", "migrate-repository.sh"):
            shutil.copyfile(BRIDGE / name, bridge / name)
        with (bridge / "unraid-common.sh").open("a") as stream:
            stream.write("\naria_preflight() { :; }\n")
        for name in ("host-agent.php", "bridge-update.php"):
            (bridge / name).write_text("<?php // Isolated host boundary.\n")
        (bridge / "host-service.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f'printf "{version} %s\\n" "$1" >> "$ARIA_TEST_SERVICES"\n'
            'case "$1" in\n'
            f' restart) printf "{version}" > "$ARIA_TEST_ACTIVE";;\n'
            f' install-boot) printf "{version}" > "$ARIA_TEST_BOOT";;\n'
            "esac\n"
        )
        git_run("init", "--initial-branch=main", str(source))
        git_run("config", "user.name", "Aria Tests", cwd=source)
        git_run("config", "user.email", "aria@example.invalid", cwd=source)
        git_run("add", ".", cwd=source)
        git_run("commit", "-qm", "initial", cwd=source)
        git_run("remote", "add", "origin", OLD_ORIGIN, cwd=source)
    remote = tmp_path / "new.git"
    git_run("clone", "--bare", str(upstream), str(remote))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, code in (("git", GIT_WRAPPER), ("docker", DOCKER_FAKE)):
        path = binaries / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)
    docker_path = tmp_path / "docker.json"
    docker_path.write_text(
        json.dumps(
            {
                "calls": [],
                "containers": {"aria-gpt-bridge": {"id": "a" * 64, "running": True}},
                "inspect": {
                    "Config": {
                        "Env": [
                            "CONTROL_PLANE_TUNNEL_ID=tunnel_original12345",
                            "ARIA_RED_URL=http://10.10.1.222:9876",
                        ]
                    },
                    "State": {"Running": True},
                    "HostConfig": {"Memory": 536870912},
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Destination": target,
                            "Source": str(data / source),
                        }
                        for source, target in (
                            ("runtime-key", "/run/secrets/control-plane-api-key"),
                            ("red-token", "/run/secrets/red_update_token"),
                            ("status", "/status"),
                        )
                    ],
                },
            }
        )
    )
    trace, active, boot = (tmp_path / name for name in ("services", "active", "boot"))
    active.write_text("old")
    boot.write_text("old")
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "ARIA_APPDATA_ROOT": str(data),
        "ARIA_TEST_REMOTE": str(remote),
        "ARIA_TEST_REAL_GIT": git,
        "ARIA_TEST_DOCKER": str(docker_path),
        "ARIA_TEST_SERVICES": str(trace),
        "ARIA_TEST_ACTIVE": str(active),
        "ARIA_TEST_BOOT": str(boot),
    }
    before = git_run("rev-parse", "HEAD", cwd=old)

    def run(**environment):
        result = subprocess.run(
            ["bash", str(old / "tools/aria_bridge/migrate-repository.sh")],
            env={**env, **environment},
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        assert "private-runtime-token" not in result.stdout + result.stderr
        assert "private-red-token" not in result.stdout + result.stderr
        assert git_run("rev-parse", "HEAD", cwd=old) == before
        assert git_run("remote", "get-url", "origin", cwd=old) == OLD_ORIGIN
        assert (data / "runtime-key").read_text() == "private-runtime-token"
        assert (data / "red-token").read_text() == "private-red-token"
        assert (state / "settings.json").read_text() == '{"keep": "settings"}'
        assert (state / "requests/existing.json").read_text() == '{"keep": "receipt"}'
        assert not list(data.glob(".repository-stage-*"))
        return result

    return SimpleNamespace(
        run=run,
        git=git_run,
        old=old,
        state=state,
        data=data,
        destination=data / "source-standalone",
        trace=trace,
        active=active,
        boot=boot,
        docker=docker_path,
    )


def test_migration_uses_new_repository_preserving_source_tunnel_and_data(migration):
    result = migration.run()
    assert result.returncode == 0, result.stderr
    assert migration.git("remote", "get-url", "origin", cwd=migration.destination) == NEW_ORIGIN
    assert migration.git("status", "--porcelain", cwd=migration.destination) == ""
    assert migration.trace.read_text().splitlines() == [
        "old stop",
        "new restart",
        "new install-boot",
    ]
    assert migration.active.read_text() == migration.boot.read_text() == "new"
    docker = json.loads(migration.docker.read_text())
    assert docker["locks_held"] == {"install.lock": True, "management/queue.lock": True}
    run = next(call for call in docker["calls"] if call[0] == "run")
    assert "CONTROL_PLANE_TUNNEL_ID=tunnel_original12345" in run
    assert "ARIA_RED_URL=http://10.10.1.222:9876" in run
    assert run[run.index("--memory") + 1] == "536870912"
    assert docker["containers"] == {"aria-gpt-bridge": {"id": "b" * 64, "running": True}}


@pytest.mark.parametrize("reason", ["directory", "symlink", "dirty", "clone", "race"])
def test_migration_refuses_existing_data_and_preflight_failures(migration, reason):
    environment = {}
    if reason == "directory":
        migration.destination.mkdir()
        (migration.destination / "keep.txt").write_text("existing directory")
    elif reason == "symlink":
        migration.destination.symlink_to(migration.old, target_is_directory=True)
    elif reason == "dirty":
        (migration.old / "local.txt").write_text("keep my edit")
    elif reason == "clone":
        environment["ARIA_TEST_CLONE_FAIL"] = "1"
    else:
        environment["ARIA_TEST_TARGET_RACE"] = "1"
    result = migration.run(**environment)
    assert result.returncode != 0
    assert not migration.trace.exists()
    assert migration.active.read_text() == migration.boot.read_text() == "old"
    if reason in {"directory", "race"}:
        assert (migration.destination / "keep.txt").exists()
    if reason == "dirty":
        assert (migration.old / "local.txt").read_text() == "keep my edit"


@pytest.mark.parametrize("status", ["queued", "running", "unknown"])
def test_migration_does_not_interrupt_pending_or_uncertain_work(migration, status):
    (migration.state / "jobs/job.json").write_text(json.dumps({"status": status}))
    result = migration.run()
    assert result.returncode != 0
    assert "must be resolved" in result.stderr
    assert not migration.destination.exists()
    assert not migration.trace.exists()


@pytest.mark.parametrize("failure", ["ARIA_TEST_BUILD_FAIL", "ARIA_TEST_RUN_FAIL"])
def test_migration_failure_restores_old_host_and_boot_and_keeps_both_sources(migration, failure):
    result = migration.run(**{failure: "1"})
    assert result.returncode != 0
    assert migration.destination.is_dir()
    assert migration.old.is_dir()
    assert migration.active.read_text() == migration.boot.read_text() == "old"
    assert migration.trace.read_text().splitlines()[-2:] == ["old restart", "old install-boot"]
    assert json.loads(migration.docker.read_text())["containers"] == {
        "aria-gpt-bridge": {"id": "a" * 64, "running": True}
    }


def test_migration_refuses_active_installer(migration):
    with (migration.data / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = migration.run()
    assert result.returncode != 0
    assert "already running" in result.stderr
    assert not migration.trace.exists()


def test_inherited_installer_lock_rejects_a_different_inode(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    wrong = tmp_path / "wrong.lock"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; aria_data="$2"; exec 7>"$3"; ARIA_INHERITED_INSTALL_LOCK=1 aria_install_lock',
            "test",
            str(BRIDGE / "unraid-common.sh"),
            str(data),
            str(wrong),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "inherited Aria installer lock is invalid" in result.stderr
