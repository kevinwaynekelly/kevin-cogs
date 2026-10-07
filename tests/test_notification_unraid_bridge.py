"""Exercise the real Unraid shell bridge with isolated notification/Docker boundaries."""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "notificationplus" / "unraid" / "script"
PRODUCER = "a" * 32


def event(seq=1, *, cog="AudioPlus", **updates):
    return {
        "seq": seq,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kind": "playback",
        "stage": "Native playback",
        "cause": "DecoderError",
        "guild_id": "765734534717112350",
        "cog": cog,
        **updates,
    }


@pytest.fixture
def bridge(tmp_path):
    for binary in ("bash", "jq", "flock", "timeout", "sync"):
        if shutil.which(binary) is None:
            pytest.skip(f"The real shell bridge requires {binary}.")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    state = tmp_path / "state"
    source = tmp_path / "data" / "NotificationPlus" / "alerts" / "unraid.json"
    source.parent.mkdir(parents=True)
    calls = tmp_path / "notifications.jsonl"
    notify = binaries / "notify"
    notify.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "with pathlib.Path(os.environ['NOTIFY_CALLS']).open('a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('NOTIFY_READY'):\n"
        "    pathlib.Path(os.environ['NOTIFY_READY']).touch()\n"
        "    while not pathlib.Path(os.environ['NOTIFY_CONTINUE']).exists():\n"
        "        time.sleep(0.01)\n"
        "sys.exit(int(os.environ.get('NOTIFY_FAILURE', '0')))\n"
    )
    notify.chmod(0o700)
    date = binaries / "date"
    date.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if os.environ.get('TEST_HOST_NOW'):\n"
        "    assert sys.argv[1:] == ['-u', '+%s']\n"
        "    print(os.environ['TEST_HOST_NOW'])\n"
        "else:\n"
        f"    os.execv({shutil.which('date')!r}, ['date'] + sys.argv[1:])\n"
    )
    date.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
        "COG_ALERTS_PATH": str(source),
        "COG_ALERTS_STATE_DIR": str(state),
        "NOTIFY_BIN": str(notify),
        "NOTIFY_CALLS": str(calls),
    }

    class Bridge:
        def write(self, events=None, **updates):
            value = {
                "schema": 1,
                "producer": PRODUCER,
                "enabled": True,
                "events": [event()] if events is None else events,
                **updates,
            }
            value.setdefault(
                "sequence",
                max(
                    (item["seq"] for item in value["events"] if type(item["seq"]) is int), default=0
                ),
            )
            source.write_text(json.dumps(value))
            return value

        def run(self, **updates):
            return subprocess.run(
                ["bash", str(SCRIPT)],
                env={**env, **updates},
                capture_output=True,
                text=True,
                timeout=25,
            )

        def calls(self):
            return (
                [json.loads(line) for line in calls.read_text().splitlines()]
                if calls.exists()
                else []
            )

    result = Bridge()
    result.env = env
    result.source = source
    result.state = state
    result.binaries = binaries
    result.tmp = tmp_path
    return result


def test_real_bridge_batches_all_cogs_and_acknowledges_only_once(bridge):
    bridge.write([event(), event(2, cog="LevelPlus", kind="command", guild_id=None)])
    before = bridge.source.read_bytes()
    result = bridge.run()
    assert result.returncode == 0, result.stderr
    call = bridge.calls()[0]
    assert call[call.index("-e") + 1] == "Kevin's Cogs"
    assert call[call.index("-i") + 1] == "alert"
    assert call[call.index("-d") + 1] == "2 new cog failure(s)."
    message = call[call.index("-m") + 1]
    assert "AudioPlus" in message and "LevelPlus" in message
    assert "765734534717112350" in message
    assert "\\n" in message and "\n" not in message
    assert bridge.source.read_bytes() == before
    cursor = bridge.state / "cursor.json"
    assert json.loads(cursor.read_text()) == {"schema": 1, "producer": PRODUCER, "seq": 2}
    assert cursor.stat().st_mode & 0o777 == 0o600
    assert bridge.state.stat().st_mode & 0o777 == 0o700
    assert bridge.run().returncode == 0
    assert len(bridge.calls()) == 1
    assert not list(bridge.state.glob(".snapshot.*"))


def test_real_bridge_new_events_and_new_producer_are_delivered(bridge):
    bridge.write()
    assert bridge.run().returncode == 0
    bridge.write([event(), event(2)])
    assert bridge.run().returncode == 0
    assert "1 new" in bridge.calls()[-1][bridge.calls()[-1].index("-d") + 1]
    bridge.write(producer="b" * 32)
    assert bridge.run().returncode == 0
    assert len(bridge.calls()) == 3


def test_real_bridge_failed_notification_retries_without_advancing_cursor(bridge):
    bridge.write()
    failed = bridge.run(NOTIFY_FAILURE="1")
    assert failed.returncode == 1 and "retry" in failed.stderr
    assert not (bridge.state / "cursor.json").exists()
    assert bridge.run().returncode == 0
    assert len(bridge.calls()) == 2
    assert bridge.run().returncode == 0
    assert len(bridge.calls()) == 2


def test_real_bridge_cursor_persistence_failure_allows_retry(bridge):
    bridge.write()
    fake_sync = bridge.binaries / "sync"
    fake_sync.write_text("#!/bin/bash\nexit 1\n")
    fake_sync.chmod(0o700)
    result = bridge.run()
    assert result.returncode == 1 and "persist" in result.stderr
    assert not (bridge.state / "cursor.json").exists()
    fake_sync.unlink()
    assert bridge.run().returncode == 0
    assert len(bridge.calls()) == 2
    assert not list(bridge.state.glob(".cursor.*"))


@pytest.mark.parametrize("updates", [{"enabled": False}, {"events": []}])
def test_real_bridge_disabled_or_empty_is_quiet(bridge, updates):
    bridge.write(**updates)
    assert bridge.run().returncode == 0
    assert bridge.calls() == []
    assert not (bridge.state / "cursor.json").exists()


def test_real_bridge_missing_snapshot_is_quiet(bridge):
    assert bridge.run().returncode == 0
    assert bridge.calls() == []
    assert not bridge.state.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"seq": True},
        {"seq": 0},
        {"seq": 1.5},
        {"seq": 9007199254740992},
        {"guild_id": True},
        {"guild_id": -1},
        {"guild_id": 765734534717112350},
        {"guild_id": "0"},
        {"guild_id": "01"},
        {"guild_id": "18446744073709551616"},
        {"guild_id": "1" * 21},
        {"at": "2026-02-31T00:00:00Z"},
        {"at": "2026-10-06T14:13:30-05:00"},
        {"kind": "unknown"},
        {"cog": "UntrustedCog"},
        {"stage": "x" * 81},
        {"cause": "x" * 241},
        {"cause": "header\nInjected newline"},
        {"cause": "private\u0000data"},
        {"cause": "https://signed.invalid/?token=private"},
        {"cause": "ftp://private.invalid/data"},
        {"cause": "www.private.invalid"},
        {"private_extra": "private"},
    ],
)
def test_real_bridge_rejects_invalid_or_unsafe_event_without_delivery(bridge, change):
    bridge.write([event(**change)])
    result = bridge.run()
    assert result.returncode == 1 and "invalid schema" in result.stderr
    assert "private" not in result.stderr
    assert bridge.calls() == []


@pytest.mark.parametrize(
    "updates",
    [
        {"schema": True},
        {"schema": "1"},
        {"producer": "not-a-uuid"},
        {"enabled": 1},
        {"sequence": True},
        {"sequence": 0},
        {"events": [event(2), event(1)]},
        {"events": [event(), event()]},
        {"events": [event(seq) for seq in range(1, 66)]},
        {"extra": "private"},
    ],
)
def test_real_bridge_rejects_invalid_snapshot(bridge, updates):
    bridge.write(**updates)
    result = bridge.run()
    assert result.returncode == 1 and "invalid schema" in result.stderr
    assert bridge.calls() == []


def test_real_bridge_read_budget_applies_before_json_validation(bridge):
    bridge.source.write_text(" " * 131073)
    result = bridge.run()
    assert result.returncode == 1 and "128 KiB" in result.stderr
    assert bridge.calls() == []


@pytest.mark.parametrize("target", ["outbox", "outbox_parent", "state", "cursor", "lock"])
def test_real_bridge_rejects_symlink_paths(bridge, target):
    bridge.write()
    if target == "outbox":
        actual = bridge.source.with_name("real.json")
        bridge.source.rename(actual)
        bridge.source.symlink_to(actual)
    elif target == "outbox_parent":
        actual = bridge.source.parent.with_name("actual")
        bridge.source.parent.rename(actual)
        bridge.source.parent.symlink_to(actual, target_is_directory=True)
    elif target == "state":
        actual = bridge.tmp / "actual"
        actual.mkdir()
        bridge.state.symlink_to(actual, target_is_directory=True)
    else:
        bridge.state.mkdir(mode=0o700)
        actual = bridge.tmp / "actual"
        actual.write_text("private")
        (bridge.state / ("cursor.json" if target == "cursor" else ".lock")).symlink_to(actual)
    result = bridge.run()
    assert result.returncode == 1
    assert bridge.calls() == []


def test_real_bridge_rejects_insecure_or_corrupt_cursor(bridge):
    bridge.write()
    bridge.state.mkdir(mode=0o700)
    cursor = bridge.state / "cursor.json"
    cursor.write_text(json.dumps({"schema": 1, "producer": PRODUCER, "seq": 0}))
    cursor.chmod(0o644)
    assert "not privately owned" in bridge.run().stderr
    cursor.chmod(0o600)
    cursor.write_text("{broken")
    assert "cursor is invalid" in bridge.run().stderr
    assert bridge.calls() == []


def test_real_bridge_limits_digest_text(bridge):
    cause = "Daily YouTube playback check failed. Run audiostatus or audiocheck now."
    bridge.write([event(seq, cause=cause) for seq in range(1, 65)])
    assert bridge.run().returncode == 0
    message = bridge.calls()[0][bridge.calls()[0].index("-m") + 1]
    assert "Plus 56 additional failures" in message
    assert len(message) < 5000
    assert json.loads((bridge.state / "cursor.json").read_text())["seq"] == 64


def test_real_bridge_rejects_shell_text_without_executing_it(bridge):
    marker = bridge.tmp / "injected"
    bridge.write([event(cause=f"$(touch {marker}) `touch {marker}`")])
    assert bridge.run().returncode == 1
    assert bridge.calls() == []
    assert not marker.exists()


def test_real_bridge_uses_the_longest_persistent_docker_mount(bridge):
    bridge.write()
    docker = bridge.binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "assert sys.argv[1:] == ['inspect', '--format', '{{json .Mounts}}', 'red-discordbot']\n"
        "print(os.environ['FAKE_MOUNTS'])\n"
    )
    docker.chmod(0o700)
    mounts = [
        {"Type": "bind", "Destination": "/data", "Source": str(bridge.tmp / "wrong")},
        {"Type": "volume", "Destination": "/data/cogs", "Source": str(bridge.tmp / "data")},
        {"Type": "tmpfs", "Destination": "/other", "Source": "/ignored"},
    ]
    result = bridge.run(
        COG_ALERTS_PATH="",
        COG_ALERTS_CONTAINER_PATH="/data/cogs/NotificationPlus/alerts/unraid.json",
        FAKE_MOUNTS=json.dumps(mounts),
    )
    assert result.returncode == 0, result.stderr
    assert len(bridge.calls()) == 1


@pytest.mark.parametrize("kind", ["tmpfs", "unknown"])
def test_real_bridge_rejects_nonpersistent_mount_masking_a_parent_bind(bridge, kind):
    bridge.write()
    docker = bridge.binaries / "docker"
    mounts = [
        {"Type": "bind", "Destination": "/data", "Source": str(bridge.tmp)},
        {"Type": kind, "Destination": "/data/NotificationPlus", "Source": None},
    ]
    docker.write_text(f"#!/bin/bash\nprintf '%s\\n' '{json.dumps(mounts)}'\n")
    docker.chmod(0o700)
    result = bridge.run(
        COG_ALERTS_PATH="",
        COG_ALERTS_CONTAINER_PATH="/data/NotificationPlus/alerts/unraid.json",
    )
    assert result.returncode == 1 and "persistent" in result.stderr
    assert bridge.calls() == []


def utc_at(seconds):
    return (
        datetime.fromtimestamp(seconds, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def test_real_bridge_skips_expired_records_and_advances_only_the_cursor(bridge):
    now = int(time.time())
    bridge.write([event(at=utc_at(now - 604801))])
    original = bridge.source.read_bytes()
    result = bridge.run(TEST_HOST_NOW=str(now))
    assert result.returncode == 0, result.stderr
    assert "expired records skipped" in result.stdout
    assert bridge.calls() == []
    assert json.loads((bridge.state / "cursor.json").read_text())["seq"] == 1
    assert bridge.source.read_bytes() == original


def test_real_bridge_batches_only_unexpired_records_and_acknowledges_expired_ones(bridge):
    now = int(time.time())
    bridge.write(
        [
            event(at=utc_at(now - 604801)),
            event(2, cog="LevelPlus", at=utc_at(now - 604800)),
        ]
    )
    assert bridge.run(TEST_HOST_NOW=str(now)).returncode == 0
    call = bridge.calls()[0]
    assert call[call.index("-d") + 1] == "1 new cog failure(s)."
    assert "AudioPlus" not in call[call.index("-m") + 1]
    assert "LevelPlus" in call[call.index("-m") + 1]
    assert json.loads((bridge.state / "cursor.json").read_text())["seq"] == 2


def test_real_bridge_retains_future_records_and_later_sequences_until_clock_catches_up(bridge):
    now = int(time.time())
    bridge.write(
        [
            event(at=utc_at(now - 604801)),
            event(2, at=utc_at(now + 30)),
            event(3, cog="LevelPlus", at=utc_at(now)),
        ]
    )
    assert bridge.run(TEST_HOST_NOW=str(now)).returncode == 0
    assert bridge.calls() == []
    assert json.loads((bridge.state / "cursor.json").read_text())["seq"] == 1
    result = bridge.run(TEST_HOST_NOW=str(now))
    assert result.returncode == 0 and "clocks agree" in result.stderr
    assert bridge.calls() == []
    assert bridge.run(TEST_HOST_NOW=str(now + 30)).returncode == 0
    assert bridge.calls()[0][bridge.calls()[0].index("-d") + 1] == "2 new cog failure(s)."
    assert json.loads((bridge.state / "cursor.json").read_text())["seq"] == 3


def test_real_bridge_docker_root_mount_is_supported(bridge):
    bridge.write()
    docker = bridge.binaries / "docker"
    mounts = [{"Type": "bind", "Destination": "/", "Source": str(bridge.tmp)}]
    docker.write_text(f"#!/bin/bash\nprintf '%s\\n' '{json.dumps(mounts)}'\n")
    docker.chmod(0o700)
    result = bridge.run(
        COG_ALERTS_PATH="",
        COG_ALERTS_CONTAINER_PATH="/data/NotificationPlus/alerts/unraid.json",
    )
    assert result.returncode == 0, result.stderr
    assert len(bridge.calls()) == 1


def test_real_bridge_serializes_concurrent_consumers(bridge):
    bridge.write()
    ready, resume = bridge.tmp / "ready", bridge.tmp / "resume"
    env = {**bridge.env, "NOTIFY_READY": str(ready), "NOTIFY_CONTINUE": str(resume)}
    worker = subprocess.Popen(
        ["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert bridge.run().returncode == 0
        assert len(bridge.calls()) == 1
        resume.touch()
        stdout, stderr = worker.communicate(timeout=5)
        assert worker.returncode == 0, (stdout, stderr)
        assert bridge.run().returncode == 0
        assert len(bridge.calls()) == 1
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)


def test_real_bridge_help_explains_email_delivery_boundary(bridge):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert "do not prove email receipt" in result.stdout
    assert bridge.calls() == []


def discovery(bridge, *, mounts=None, relative="instance"):
    root = bridge.tmp / "persistent"
    source = root / relative / "cogs" / "NotificationPlus" / "alerts" / "unraid.json"
    source.parent.mkdir(parents=True)
    source.write_text(bridge.source.read_text())
    docker = bridge.binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "assert sys.argv[1:] == ['inspect', '--format', '{{json .Mounts}}', 'red-discordbot']\n"
        "print(os.environ['FAKE_MOUNTS'])\n"
    )
    docker.chmod(0o700)
    env = dict(
        COG_ALERTS_PATH="",
        COG_ALERTS_CONTAINER_PATH="",
        FAKE_MOUNTS=json.dumps(
            mounts or [{"Type": "bind", "Destination": "/data", "Source": str(root)}]
        ),
    )
    return source, env


@pytest.mark.parametrize("relative", ["", "instance", "red/instance"])
def test_bridge_discovers_one_outbox_without_reading_config(bridge, relative):
    bridge.write()
    source, env = discovery(bridge, relative=relative)
    private = source.parents[2] / "Core" / "settings.json"
    private.parent.mkdir()
    private.write_text("not JSON and never read")
    result = bridge.run(**env)
    assert result.returncode == 0, result.stderr
    assert len(bridge.calls()) == 1
    assert bridge.run(**env).returncode == 0
    assert len(bridge.calls()) == 1


def test_bridge_discovery_waits_for_cog_install(bridge):
    bridge.write()
    source, env = discovery(bridge)
    source.unlink()
    result = bridge.run(**env)
    assert result.returncode == 0 and "Waiting for NotificationPlus" in result.stdout
    assert not bridge.calls()


def test_bridge_discovery_requires_selection_with_multiple_instances(bridge):
    bridge.write()
    source, env = discovery(bridge)
    discovery(bridge, relative="second")
    result = bridge.run(**env)
    assert result.returncode == 1 and "Multiple" in result.stderr
    assert not bridge.calls()
    env["COG_ALERTS_CONTAINER_PATH"] = "/data/instance/cogs/NotificationPlus/alerts/unraid.json"
    assert bridge.run(**env).returncode == 0
    assert len(bridge.calls()) == 1


@pytest.mark.parametrize("kind", ["tmpfs", "bind"])
def test_bridge_discovery_rejects_shadowed_files(bridge, kind):
    bridge.write()
    source, env = discovery(bridge)
    mounts = json.loads(env["FAKE_MOUNTS"])
    mounts.append(
        {"Type": kind, "Destination": "/data/instance/cogs", "Source": str(bridge.tmp / "other")}
    )
    env["FAKE_MOUNTS"] = json.dumps(mounts)
    result = bridge.run(**env)
    assert result.returncode == 1 and ("masked" in result.stderr or "shadowed" in result.stderr)
    assert not bridge.calls()


def test_bridge_discovery_rejects_symlinked_instance(bridge):
    bridge.write()
    source, env = discovery(bridge)
    root = source.parents[4]
    instance = root / "instance"
    moved = root / "moved"
    instance.rename(moved)
    instance.symlink_to(moved, target_is_directory=True)
    result = bridge.run(**env)
    assert result.returncode == 1 and "symlink" in result.stderr
    assert not bridge.calls()
