"""Exercise host upgrades with a stateful fake Docker, never a live daemon."""

import fnmatch
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "tools/aria_bridge"


FAKE_DOCKER = r"""
import json, os, sys
from pathlib import Path
state_file = Path(os.environ["ARIA_TEST_DOCKER_STATE"])
state = json.loads(state_file.read_text())
args = sys.argv[1:]
state["calls"].append(args)
rc = 0
result = ""
if args[0] == "inspect" and "--format" not in args:
    result = json.dumps([state["inspect"]])
elif args[0] == "inspect":
    if args[args.index("--format") + 1] == "{{json .State}}":
        result = json.dumps({"Status": "exited", "ExitCode": 1})
    elif args[args.index("--format") + 1].startswith("{{.Id}}"):
        current = state["containers"]["aria-gpt-bridge"]
        identifier = {"new": "c", "old": "a", "foreign": "f"}[current["version"]]
        result = identifier * 64 + " <no value>"
    else:
        result = "running " + ("unhealthy" if state.get("bad_health") else "healthy")
elif args[:2] == ["container", "inspect"]:
    rc = 0 if args[-1] in state["containers"] else 1
elif args[0] == "build":
    rc = 19 if state.get("build_fails") else 0
elif args[0] == "logs":
    result = "PermissionError: management.py; provider-secret-never-print"
elif args[0] == "stop":
    state["containers"][args[-1]]["running"] = False
elif args[0] == "rename":
    if state.get("rename_fails") and args[1] == "aria-gpt-bridge":
        rc = 23
    else:
        state["containers"][args[2]] = state["containers"].pop(args[1])
elif args[0] == "run":
    if state.get("foreign_on_run"):
        state["containers"]["aria-gpt-bridge"] = {"running": True, "version": "foreign"}
        rc = 17
    elif state.get("run_fails"):
        rc = 17
    else:
        state["containers"]["aria-gpt-bridge"] = {"running": True, "version": "new"}
        result = "c" * 64
elif args[0] == "start":
    state["containers"][args[-1]]["running"] = True
elif args[0] == "rm":
    name = args[-1]
    if name == "c" * 64: name = "aria-gpt-bridge"
    if state.get("backup_cleanup_fails") and name.startswith("aria-gpt-bridge-rollback-"):
        rc = 31
    else:
        state["containers"].pop(name, None)
elif args[0] == "update":
    if state.get("disable_restart_fails"):
        rc = 32
    else:
        state["containers"][args[-1]]["restart"] = "no"
elif args[0] != "info":
    rc = 41
state_file.write_text(json.dumps(state))
if result:
    print(result)
sys.exit(rc)
"""


@pytest.fixture
def upgrade_host(tmp_path):
    if not shutil.which("jq"):
        pytest.skip("Host installer requires jq")
    source = tmp_path / "persistent source"
    source.mkdir()
    for name in ("upgrade-unraid.sh", "unraid-common.sh", "host-service.sh"):
        shutil.copyfile(BRIDGE / name, source / name)
    # Replace only platform detection in this isolated fixture. Production still
    # requires root, native Unraid PHP, the template converter and persistent paths.
    with (source / "unraid-common.sh").open("a") as output:
        output.write("\naria_preflight() { :; }\n")
    host_service = source / "host-service.sh"
    host_service.write_text('#!/bin/bash\nprintf "%s\\n" "$1" >> "$ARIA_TEST_SERVICE_CALLS"\n')
    data = tmp_path / "appdata"
    data.mkdir()
    status = data / "status"
    status.mkdir()
    key = data / "runtime-key"
    key.write_text("runtime-secret-never-print")
    token = data / "red-token"
    token.write_text("red-secret-never-print")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    docker.chmod(0o755)
    state = {
        "calls": [],
        "containers": {"aria-gpt-bridge": {"running": True, "version": "old"}},
        "inspect": {
            "Config": {
                "Env": [
                    "CONTROL_PLANE_TUNNEL_ID=tunnel_original12345",
                    "ARIA_RED_URL=http://10.10.1.222:9876",
                    "SHOULD_NOT_COPY=private-env-value",
                ]
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": destination,
                    "Source": str(path),
                }
                for destination, path in (
                    ("/run/secrets/control-plane-api-key", key),
                    ("/run/secrets/red_update_token", token),
                    ("/status", status),
                )
            ],
            "State": {"Running": True},
            "HostConfig": {
                "Memory": 536870912,
                "MemorySwap": 1073741824,
                "PidsLimit": 128,
                "NanoCpus": 1500000000,
                "CpuShares": 1024,
                "CpusetCpus": "1,2",
            },
        },
    }
    state_file = tmp_path / "docker.json"
    service_calls = tmp_path / "service-calls"

    def run(env_overrides=None, **changes):
        state.update(changes)
        state_file.write_text(json.dumps(state))
        result = subprocess.run(
            ["bash", str(source / "upgrade-unraid.sh")],
            env={
                **os.environ,
                "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "ARIA_APPDATA_ROOT": str(data),
                "ARIA_TEST_DOCKER_STATE": str(state_file),
                "ARIA_TEST_SERVICE_CALLS": str(service_calls),
                **(env_overrides or {}),
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result, json.loads(state_file.read_text()), service_calls

    return run


def test_upgrade_keeps_actual_settings_credentials_and_limits(upgrade_host):
    result, state, service_calls = upgrade_host()
    assert result.returncode == 0, result.stderr
    run = next(call for call in state["calls"] if call[0] == "run")
    assert "CONTROL_PLANE_TUNNEL_ID=tunnel_original12345" in run
    assert "ARIA_RED_URL=http://10.10.1.222:9876" in run
    assert "ARIA_AGENT_SOCKET=/run/aria-agent/agent.sock" in run
    for flag, value in (
        ("--memory", "536870912"),
        ("--memory-swap", "1073741824"),
        ("--pids-limit", "128"),
        ("--cpus", "1.5"),
        ("--cpu-shares", "1024"),
        ("--cpuset-cpus", "1,2"),
        ("--user", "65532:65532"),
    ):
        assert run[run.index(flag) + 1] == value
    assert "/var/run/aria-gpt-bridge:/run/aria-agent:ro" in run
    assert not any("docker.sock" in argument for argument in run)
    assert not any("SHOULD_NOT_COPY" in argument for argument in run)
    for secret in ("runtime-secret-never-print", "red-secret-never-print", "private-env-value"):
        assert secret not in result.stdout + result.stderr + json.dumps(state["calls"])
    assert service_calls.read_text().splitlines() == ["restart", "install-boot"]
    assert state["containers"] == {"aria-gpt-bridge": {"version": "new", "running": True}}
    actions = [call[0] for call in state["calls"]]
    assert actions.index("build") < actions.index("stop") < actions.index("run")


def test_failed_build_preserves_container_and_host_service(upgrade_host):
    result, state, service_calls = upgrade_host(build_fails=True)
    assert result.returncode != 0
    assert not service_calls.exists()
    assert not any(call[0] in {"stop", "rename", "run", "rm"} for call in state["calls"])
    assert state["containers"] == {"aria-gpt-bridge": {"version": "old", "running": True}}


@pytest.mark.parametrize("failure", ["run_fails", "bad_health", "rename_fails"])
def test_failed_upgrade_restores_original_running_container(upgrade_host, failure):
    result, state, _ = upgrade_host(**{failure: True})
    assert result.returncode != 0
    assert "restoring the previous bridge" in result.stderr
    assert state["containers"] == {"aria-gpt-bridge": {"version": "old", "running": True}}


def test_failed_startup_retains_private_diagnostics_before_rollback(upgrade_host):
    result, state, service_calls = upgrade_host(bad_health=True)
    assert result.returncode != 0
    assert "failed its health check" in result.stderr
    logs = list((service_calls.parent / "appdata/management").glob("upgrade-failure.*.log"))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert "PermissionError: management.py" in logs[0].read_text()
    assert "provider-secret-never-print" not in result.stdout + result.stderr
    assert str(logs[0]) in result.stderr
    calls = [call[0] for call in state["calls"]]
    assert calls.index("logs") < calls.index("rm") < calls.index("start")
    assert state["containers"] == {"aria-gpt-bridge": {"version": "old", "running": True}}


def test_upgrade_rollback_preserves_concurrent_foreign_bridge(upgrade_host):
    result, state, _ = upgrade_host(foreign_on_run=True)
    assert result.returncode != 0
    assert state["containers"]["aria-gpt-bridge"]["version"] == "foreign"
    backups = [name for name in state["containers"] if name.startswith("aria-gpt-bridge-rollback-")]
    assert len(backups) == 1
    assert state["containers"][backups[0]]["version"] == "old"
    assert not any(call[0] == "rm" for call in state["calls"])


@pytest.mark.parametrize("restart_failure", [False, True])
def test_healthy_upgrade_keeps_success_when_stopped_backup_cleanup_fails(
    upgrade_host, restart_failure
):
    result, state, _ = upgrade_host(
        backup_cleanup_fails=True, disable_restart_fails=restart_failure
    )
    assert result.returncode == 0, result.stderr
    assert state["containers"]["aria-gpt-bridge"] == {"running": True, "version": "new"}
    backups = [
        container
        for name, container in state["containers"].items()
        if name.startswith("aria-gpt-bridge-rollback-")
    ]
    assert len(backups) == 1
    assert backups[0]["version"] == "old" and not backups[0]["running"]
    assert any(call[:2] == ["update", "--restart=no"] for call in state["calls"])
    if restart_failure:
        assert "restart policy could not be disabled" in result.stderr
    else:
        assert backups[0]["restart"] == "no"
        assert "restart disabled" in result.stderr


def test_remote_upgrade_labels_its_replacement_container(upgrade_host):
    result, state, _ = upgrade_host(env_overrides={"ARIA_BRIDGE_UPDATE_ID": "trusted_internal_job"})
    assert result.returncode == 0, result.stderr
    run = next(call for call in state["calls"] if call[0] == "run")
    assert run[run.index("--label") + 1] == "com.aria-gpt-bridge.update-id=trusted_internal_job"


def test_host_boot_hook_is_idempotent_and_preserves_original(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("Host launcher requires root for its group ownership")
    source = tmp_path / "source with spaces"
    source.mkdir()
    common = (BRIDGE / "unraid-common.sh").read_text()
    (source / "unraid-common.sh").write_text(common + "\naria_preflight() { :; }\n")
    boot_root = tmp_path / "boot"
    (boot_root / "config").mkdir(parents=True)
    go = boot_root / "config/go"
    original = "#!/bin/bash\n# Existing Unraid startup\n"
    go.write_text(original)
    runtime = tmp_path / "runtime"
    service = (BRIDGE / "host-service.sh").read_text()
    service = service.replace("/boot/", str(boot_root) + "/")
    service = service.replace("/var/run/aria-gpt-bridge", str(runtime))
    # Some test user namespaces do not map Unraid's bridge GID.
    service = service.replace('chown root:65532 "$aria_runtime"', "true")
    service_file = source / "host-service.sh"
    service_file.write_text(service)
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(service_file), "install-boot"],
            env={**os.environ, "ARIA_APPDATA_ROOT": str(tmp_path / "appdata")},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert go.read_text().startswith(original)
    assert go.read_text().count("# Aria GPT host management") == 1
    backups = list(go.parent.glob("go.aria-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    boot = boot_root / "config/plugins/aria-gpt-bridge/boot.sh"
    syntax = subprocess.run(["bash", "-n", str(boot)], capture_output=True, check=False)
    assert syntax.returncode == 0, syntax.stderr
    assert "source\\ with\\ spaces" in boot.read_text()
    assert runtime.stat().st_mode & 0o777 == 0o750
    assert (tmp_path / "appdata/management").stat().st_mode & 0o777 == 0o700


def test_container_image_includes_management_module():
    dockerfile = (BRIDGE / "Dockerfile").read_text()
    assert "COPY server.py management.py entrypoint.sh *-tools.json ./" in dockerfile
    assert "management.request" in dockerfile
    assert "docker.sock" not in dockerfile


def test_image_runtime_permissions_from_private_checkout(tmp_path):
    # Exercise the real image chmod command against a 0600 build context. No
    # Docker daemon is used here; CI separately builds and imports as UID 65532.
    app = tmp_path / "app with spaces"
    app.mkdir()
    sources = [BRIDGE / name for name in ("server.py", "management.py", "entrypoint.sh")]
    sources.extend(BRIDGE.glob("*-tools.json"))
    for source in sources:
        target = app / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
    dockerfile = (BRIDGE / "Dockerfile").read_text().replace("\\\n", " ")
    for line in dockerfile.splitlines():
        if line.startswith("RUN chmod "):
            command = line.removeprefix("RUN ").replace("/app/", shlex.quote(str(app) + "/"))
            subprocess.run(["sh", "-c", command], check=True, capture_output=True, timeout=10)
    for source in sources:
        mode = (app / source.name).stat().st_mode
        assert mode & 0o004, f"Container UID cannot read {source.name}"
        assert not mode & 0o222, f"Runtime code must be read-only: {source.name}"
    assert (app / "entrypoint.sh").stat().st_mode & 0o001
    assert dockerfile.index("USER 65532:65532") < dockerfile.index(
        'RUN python -c "import management, server"'
    )


def service_function_script(tmp_path, suffix):
    """Load real service functions without invoking Unraid startup or root setup."""
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(BRIDGE / "unraid-common.sh", source / "unraid-common.sh")
    functions = (BRIDGE / "host-service.sh").read_text().split("\naria_preflight\n", 1)[0]
    script = source / "host-service-test.sh"
    script.write_text(functions + "\n" + suffix)
    data = tmp_path / "appdata"
    (data / "management").mkdir(parents=True)
    return script, data


def test_supervisor_wait_does_not_retain_lock_after_shutdown(tmp_path):
    if not shutil.which("setsid"):
        pytest.skip("Host process supervision requires setsid")
    # PID identity is separately tested on native /proc. This test must also run
    # where the sandbox's visible /proc and signal PID namespaces differ.
    script, data = service_function_script(
        tmp_path, 'aria_pid_start() { printf "1\\n"; }\naria_supervise work\n'
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    php = binaries / "php"
    php.write_text("#!/bin/bash\nexec /bin/sleep 300\n")
    php.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text(
        '#!/bin/bash\nif [[ "$1" == 2 ]]; then\n'
        '  printf "%s\\n" "$$" > "$ARIA_APPDATA_ROOT/wait.pid"\n'
        "  exec /bin/sleep 30\n"
        "fi\nexec /bin/sleep 0.01\n"
    )
    sleep.chmod(0o755)
    env = {
        **os.environ,
        "ARIA_APPDATA_ROOT": str(data),
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
    }
    supervisor = subprocess.Popen(
        ["bash", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    waiting_pid = None
    try:
        waiting_file = data / "wait.pid"
        for _ in range(250):
            if waiting_file.exists() and waiting_file.read_text().strip():
                break
            time.sleep(0.01)
        assert waiting_file.exists(), supervisor.poll()
        waiting_pid = int(waiting_file.read_text())
        supervisor.terminate()
        supervisor.wait(timeout=5)
        assert supervisor.returncode == 0
        # The wait is deliberately still alive. It must not own the old
        # supervisor lock or prevent a replacement supervisor from starting.
        os.kill(waiting_pid, 0)
        lock = subprocess.run(
            ["flock", "-n", str(data / "management/work.supervisor.lock"), "true"],
            capture_output=True,
            timeout=2,
            check=False,
        )
        assert lock.returncode == 0, "Background sleep retained the supervisor lock"
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
        if waiting_pid is not None:
            try:
                os.kill(waiting_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_stop_waits_for_old_supervisor_lock_after_pid_disappears(tmp_path):
    script, data = service_function_script(
        tmp_path,
        "aria_alive() { return 1; }\naria_stop_role work\n"
        'flock -n "$aria_state/work.supervisor.lock" true\n',
    )
    lock = data / "management/work.supervisor.lock"
    ready = data / "lock-ready"
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'exec 8> "$1"; flock 8; touch "$2"; sleep 0.5',
            "old-supervisor-wait",
            str(lock),
            str(ready),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(250):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists()
        result = subprocess.run(
            ["bash", str(script)],
            env={**os.environ, "ARIA_APPDATA_ROOT": str(data)},
            capture_output=True,
            timeout=7,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    finally:
        holder.wait(timeout=2)


def test_all_docker_copy_sources_are_in_build_context():
    # This context uses a flat deny-all allowlist. Test the actual COPY inputs,
    # so adding a runtime file cannot silently leave it out of Docker's context.
    rules = [
        line.strip()
        for line in (BRIDGE / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert rules[0] == "*"
    sources = []
    for line in (BRIDGE / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            sources.extend(shlex.split(line)[1:-1])
    assert sources
    for pattern in sources:
        matches = list(BRIDGE.glob(pattern))
        assert matches, f"Missing COPY source: {pattern}"
        for path in matches:
            assert path.is_file()
            source = path.name
            included = True
            for rule in rules:
                if fnmatch.fnmatchcase(source, rule.removeprefix("!")):
                    included = rule.startswith("!")
            assert included, f"Docker COPY source excluded by .dockerignore: {source}"


@pytest.mark.parametrize("role", ["work", "automation", "serve"])
@pytest.mark.parametrize("record_present", [True, False])
def test_service_shutdown_terminates_timeout_group_and_prevents_duplicates(
    tmp_path, role, record_present
):
    if os.geteuid() != 0 or not shutil.which("setsid"):
        pytest.skip("Host process supervision requires root and setsid")
    if int(Path("/proc/self/stat").read_text().split()[0]) != os.getpid():
        pytest.skip("The sandbox exposes host /proc IDs from a different PID namespace")
    source = tmp_path / "source"
    source.mkdir()
    (source / "unraid-common.sh").write_text(
        (BRIDGE / "unraid-common.sh").read_text() + "\naria_preflight() { :; }\n"
    )
    service = (BRIDGE / "host-service.sh").read_text()
    service = service.replace("/var/run/aria-gpt-bridge", str(tmp_path / "runtime"))
    service = service.replace('chown root:65532 "$aria_runtime"', "true")
    (source / "host-service.sh").write_text(service)
    # Keep the real automation entrypoint: its exec must leave PHP as the
    # supervisor's direct child, including the pre-record shutdown race.
    (source / "automation-service.sh").write_text((BRIDGE / "automation-service.sh").read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    php = binaries / "php"
    php.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "role = os.environ['ARIA_TEST_SUPERVISOR_ROLE']\n"
        "state = Path(sys.argv[-2] if role == 'serve' else sys.argv[-1])\n"
        'child = subprocess.Popen(["timeout", "300", "bash", "-c", "sleep 300"])\n'
        "for _ in range(100):\n"
        "    if os.getpgid(child.pid) == child.pid: break\n"
        "    time.sleep(0.01)\n"
        'stat = Path(f"/proc/{child.pid}/stat").read_text().rsplit(") ", 1)[1].split()\n'
        "record = {'work':'active-process.json', 'automation':'active-automation-process.json', 'serve':'active-serve-process.json'}[role]\n"
        '(state / record).write_text(json.dumps({"pid": child.pid, "start_time": stat[19]}))\n'
        'children = Path(f"/proc/{child.pid}/task/{child.pid}/children")\n'
        "for _ in range(100):\n"
        "    descendants = children.read_text().split()\n"
        "    if descendants: break\n"
        "    time.sleep(0.01)\n"
        '(state / "test-pids.json").write_text(json.dumps([os.getpid(), child.pid] + [int(p) for p in descendants]))\n'
        "time.sleep(300)\n"
    )
    php.chmod(0o755)
    data = tmp_path / "appdata"
    env = {
        **os.environ,
        "ARIA_APPDATA_ROOT": str(data),
        "ARIA_TEST_SUPERVISOR_ROLE": role,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
    }
    args = ["bash", str(source / "host-service.sh"), "_supervise", role]
    supervisor = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        pids_file = data / "management/test-pids.json"
        for _ in range(250):
            if pids_file.exists():
                break
            time.sleep(0.02)
        assert pids_file.exists(), supervisor.poll()
        pids = json.loads(pids_file.read_text())
        assert len(pids) >= 3
        duplicate = subprocess.run(args, env=env, capture_output=True, timeout=5, check=False)
        assert duplicate.returncode == 0, duplicate.stderr
        assert supervisor.poll() is None
        if not record_present:
            # Reproduce shutdown before PHP can publish proc_open's child PID.
            record = {
                "work": "active-process.json",
                "automation": "active-automation-process.json",
                "serve": "active-serve-process.json",
            }[role]
            (data / "management" / record).unlink()
        supervisor.terminate()
        supervisor.communicate(timeout=15)
        assert supervisor.returncode == 0
        for pid in pids:
            stat = Path(f"/proc/{pid}/stat")
            assert not stat.exists() or stat.read_text().rsplit(") ", 1)[1].split()[0] == "Z"
        assert not (data / f"management/{role}.pid").exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.communicate(timeout=5)
