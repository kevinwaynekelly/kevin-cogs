"""Real PHP scheduling and durable queue tests, with local fake Git/GitHub boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHP = shutil.which("php")
HOST = ROOT / "tools/aria_bridge/host-agent.php"
AUTOMATION = ROOT / "tools/aria_bridge/host-automation.php"
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")
RUNNER = r"""
require_once $argv[1]; require_once $argv[2];
$a = json_decode(stream_get_contents(STDIN), true); ariaInit($a['state']);
try {
    if ($a['mode'] === 'tick') $r = ariaAutomationTick($a['state']);
    elseif ($a['mode'] === 'check') $r = ariaAutomationBridgeCheck($a['state'], $a['install'] ?? false);
    elseif ($a['mode'] === 'slot') $r = ['slot' => ariaAutomationSlot($a['schedule'], $a['now']), 'window' => ariaAutomationWindow($a['schedule'], $a['now'])];
    elseif ($a['mode'] === 'script') $r = ariaExecute($a['state'], ariaReadJson($a['state'].'/jobs/'.$a['job_id'].'.json'));
    else {
        $args = ariaAutomationValidate($a['action'], $a['arguments']);
        if ($a['mode'] === 'execute') $r = ariaAutomationExecute($a['state'], ['action' => $a['action'], 'arguments' => $args, 'job_id' => 'control-test']);
        elseif ($a['mode'] === 'control') $r = ariaAutomationControl($a['state'], $a['action'], $args);
        else $r = ariaAutomationRead($a['state'], $a['action'], $args);
    }
    echo ariaJson(['ok' => true, 'result' => $r]);
} catch (Throwable $e) { echo ariaJson(['ok' => false, 'error' => ariaError($e)]); }
"""


@pytest.fixture
def automation(tmp_path):
    state = tmp_path / "state"
    scripts = tmp_path / "scripts"
    script = scripts / "Backup" / "script"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho 'backup complete'\n")
    source = tmp_path / "source"
    source.mkdir()
    now = int(time.time())
    env = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_AGENT_SCRIPTS": str(scripts),
        "ARIA_AUTOMATION_SOURCE": str(source),
        "ARIA_AUTOMATION_TIME": str(now),
    }

    class Automation:
        def call(self, mode="read", action="automation_status", **kwargs):
            payload = {"state": str(state), "mode": mode, "action": action, **kwargs}
            payload.setdefault("arguments", {})
            result = subprocess.run(
                [PHP, "-r", RUNNER, str(HOST), str(AUTOMATION)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                check=True,
                timeout=20,
            )
            return json.loads(result.stdout)

        def save(self, **kwargs):
            args = dict(
                id="daily-backup",
                action="script_run",
                arguments={
                    "name": "Backup",
                    "expected_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                    "timeout_seconds": 60,
                },
                trigger="interval",
                interval_minutes=1,
                expected_sha256="",
                request_id="save-backup",
            )
            args.update(kwargs)
            return self.call("execute", "automation_schedule_save", arguments=args)

        def jobs(self):
            return [json.loads(p.read_text()) for p in sorted((state / "jobs").glob("*.json"))]

        def tick(self):
            return self.call("tick")

        def advance(self, seconds):
            env["ARIA_AUTOMATION_TIME"] = str(int(env["ARIA_AUTOMATION_TIME"]) + seconds)

        def seed_job(self, ident="source-job", **kwargs):
            self.call()
            job = dict(
                job_id=ident,
                action="operations_archive_create",
                status="succeeded",
                created_at="2099-01-01T00:00:00Z",
                started_at="2099-01-01T00:00:00Z",
                finished_at="2099-01-01T00:00:01Z",
                result={},
            )
            job.update(kwargs)
            (state / "jobs" / f"{ident}.json").write_text(json.dumps(job))
            return job

        def fake_git(self, **overrides):
            config = {
                "head": "a" * 40,
                "target": "b" * 40,
                "origin": "https://github.com/kevinwaynekelly/aria-gpt-bridge.git",
                "branch": "main",
                "dirty": "",
                "changed": "tools/aria_bridge/server.py",
            }
            config.update(overrides)
            git_config = tmp_path / "git.json"
            git_config.write_text(json.dumps(config))
            executable = tmp_path / "fake-git"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os,json,sys\n"
                "from pathlib import Path\n"
                "c=json.loads(Path(os.environ['FAKE_GIT_CONFIG']).read_text())\n"
                "a=sys.argv[sys.argv.index('-C')+2:]\n"
                "if a[:2]==['rev-parse','--show-toplevel']: print(os.environ['ARIA_AUTOMATION_SOURCE'])\n"
                "elif a[0]=='symbolic-ref': print(c['branch'])\n"
                "elif a[0]=='status': print(c['dirty'],end='')\n"
                "elif a[0]=='remote': print(c['origin'])\n"
                "elif a[0]=='rev-parse': print(c['head'] if 'HEAD' in a[-1] else c['target'])\n"
                "elif a[0]=='diff': print(c['changed'])\n"
                "elif a[0]=='merge-base': sys.exit(c.get('ancestor_exit',0))\n"
                "elif a[0]!='fetch': sys.exit(1)\n"
            )
            executable.chmod(0o700)
            env.update(ARIA_AUTOMATION_GIT=str(executable), FAKE_GIT_CONFIG=str(git_config))
            curl_file = tmp_path / "checks.json"
            checks = [
                dict(
                    id=n,
                    name=f"checks ({version})",
                    head_sha=config["target"],
                    app={"slug": "github-actions"},
                    status="completed",
                    conclusion="success",
                    details_url="https://github.com/kevinwaynekelly/aria-gpt-bridge/actions/runs/1/job/2",
                )
                for n, version in enumerate(("3.10", "3.11"), 1)
            ]
            curl_file.write_text(json.dumps({"total_count": len(checks), "check_runs": checks}))
            curl = tmp_path / "fake-curl"
            curl.write_text(
                "#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
                "print(Path(os.environ['FAKE_CHECKS']).read_text())\n"
            )
            curl.chmod(0o700)
            env.update(ARIA_AUTOMATION_CURL=str(curl), FAKE_CHECKS=str(curl_file))
            return config, curl_file

    instance = Automation()
    instance.state = state
    instance.env = env
    instance.script = script
    instance.now = now
    return instance


def test_default_is_only_bridge_update_and_delayed_24_hours(automation):
    result = automation.call(action="automation_schedules")["result"]
    schedules = result["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["action"] == "bridge_auto_update"
    assert schedules[0]["not_before"] == automation.now + 86400
    assert schedules[0]["window_timezone"] == "America/Chicago"
    assert automation.tick()["ok"]
    assert automation.jobs() == []


def test_schedule_cas_and_redaction(automation):
    saved = automation.save()
    assert saved["ok"]
    schedule = saved["result"]
    assert "arguments" not in schedule
    assert schedule["argument_fields"] == ["name", "expected_sha256", "timeout_seconds"]
    assert automation.save()["error"] == "hash mismatch"
    assert automation.save(expected_sha256=schedule["sha256"], enabled=False)["ok"]
    assert (
        automation.call(
            "execute",
            "automation_schedule_delete",
            arguments=dict(
                id="daily-backup", expected_sha256=schedule["sha256"], request_id="delete"
            ),
        )["error"]
        == "hash mismatch"
    )


def test_tick_deduplicates_same_slot_after_lost_runtime(automation):
    assert automation.save()["ok"]
    automation.advance(61)
    assert automation.tick()["ok"]
    assert len(automation.jobs()) == 1
    job = automation.jobs()[0]
    job["status"] = "succeeded"
    job.pop("arguments")
    (automation.state / "jobs" / f"{job['job_id']}.json").write_text(json.dumps(job))
    (automation.state / "automation/runtime.json").unlink()
    assert automation.tick()["ok"]
    assert len(automation.jobs()) == 1
    assert automation.call()["result"]["runtime"]["daily-backup"]["last_result"]["deduplicated"]


def test_schedule_blocks_changed_script_before_queue(automation):
    assert automation.save()["ok"]
    automation.script.write_text("echo changed\n")
    automation.advance(61)
    assert automation.tick()["ok"]
    assert automation.jobs() == []
    runtime = automation.call()["result"]["runtime"]["daily-backup"]
    assert runtime["blocked_error"] == "hash mismatch"
    assert len(automation.call(action="automation_inbox")["result"]["notifications"]) == 1


@pytest.mark.parametrize("status", ["running", "queued", "unknown"])
def test_schedules_defer_while_management_is_busy(automation, status):
    assert automation.save()["ok"]
    automation.seed_job(status=status)
    automation.advance(61)
    automation.tick()
    assert len(automation.jobs()) == 1
    assert (
        automation.call()["result"]["runtime"]["daily-backup"]["deferred_reason"]
        == "management busy"
    )


def test_cancellation_is_immediate_and_idempotent(automation):
    automation.seed_job(status="queued", arguments={"sensitive": "secret"})
    args = dict(job_id="source-job", request_id="cancel-once")
    result = automation.call("control", "automation_job_cancel", arguments=args)
    assert result["result"]["status"] == "cancelled"
    assert automation.call("control", "automation_job_cancel", arguments=args)["result"][
        "deduplicated"
    ]
    target = json.loads((automation.state / "jobs/source-job.json").read_text())
    assert "arguments" not in target
    assert len(automation.jobs()) == 2


@pytest.mark.parametrize("status", ["running", "succeeded", "unknown", "cancelled"])
def test_cancellation_refuses_nonqueued_jobs(automation, status):
    automation.seed_job(status=status)
    assert (
        automation.call(
            "control",
            "automation_job_cancel",
            arguments=dict(job_id="source-job", request_id="cancel"),
        )["error"]
        == "management busy"
    )
    assert len(automation.jobs()) == 1


def test_event_fires_once_and_refuses_self_loop(automation):
    assert automation.save(trigger="event", event_action="operations_archive_create")["ok"]
    automation.seed_job(automation={"chain": ["daily-backup"]})
    automation.advance(61)
    automation.tick()
    assert len(automation.jobs()) == 1
    automation.seed_job(ident="new-backup")
    automation.tick()
    assert len(automation.jobs()) == 3
    automation.tick()
    assert len(automation.jobs()) == 3


def test_notifications_are_bounded_and_dashboard_escapes(automation):
    automation.seed_job(action="<script>alert(1)</script>", status="failed")
    automation.tick()
    notice = automation.call(action="automation_inbox")["result"]["notifications"][0]
    html = automation.call(action="automation_dashboard")["result"]["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert automation.call(
        "execute", "automation_inbox_ack", arguments=dict(id=notice["id"], request_id="ack")
    )["ok"]
    assert automation.call(action="automation_inbox")["result"]["notifications"] == []
    automation.tick()
    assert automation.call(action="automation_inbox")["result"]["notifications"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"action": "host_exec"},
        {"action": "automation_schedule_save"},
        {"time_zone": "../../UTC"},
        {"window_start": "02:00", "window_end": ""},
        {"interval_minutes": 0},
        {"arguments": {"name": "Backup", "request_id": "injected"}},
    ],
)
def test_invalid_schedule_cannot_bypass_target_validation(automation, change):
    assert automation.save(**change)["error"] == "invalid request"


def test_overnight_window_and_daily_dst_slot(automation):
    schedule = automation.save(
        trigger="daily", daily_time="01:30", window_start="23:00", window_end="02:00"
    )["result"]
    # Both occurrences of 01:30 during autumn fallback have the same daily slot.
    for timestamp in (1793514600, 1793518200):
        result = automation.call("slot", schedule=schedule, now=timestamp)["result"]
        assert result["slot"] == "2026-11-01"
        assert result["window"]


def test_bridge_check_requires_both_successful_checks(automation):
    automation.call()
    _, checks_file = automation.fake_git()
    result = automation.call("check")["result"]
    assert result["status"] == "available" and result["ci_verified"]
    checks = json.loads(checks_file.read_text())
    checks["check_runs"][0]["conclusion"] = "failure"
    checks_file.write_text(json.dumps(checks))
    result = automation.call("check")["result"]
    assert result["status"] == "ci_pending" and not result["ci_verified"]


def test_new_failed_ci_rerun_overrides_old_success(automation):
    automation.call()
    _, checks_file = automation.fake_git()
    checks = json.loads(checks_file.read_text())
    checks["check_runs"].append({**checks["check_runs"][0], "id": 100, "conclusion": "failure"})
    checks["total_count"] = 3
    checks_file.write_text(json.dumps(checks))
    assert automation.call("check")["result"]["status"] == "ci_pending"


@pytest.mark.parametrize(
    "flags,status",
    [
        ({"head": "b" * 40}, "current"),
        ({"changed": ""}, "no_bridge_changes"),
        ({"origin": "https://github.com/attacker/aria-gpt-bridge.git"}, "check_failed"),
        ({"branch": "feature"}, "check_failed"),
        ({"dirty": " M file"}, "check_failed"),
        ({"ancestor_exit": 1}, "check_failed"),
    ],
)
def test_bridge_check_fixed_repository_clean_main_and_relevance(automation, flags, status):
    automation.call()
    automation.fake_git(**flags)
    assert automation.call("check")["result"]["status"] == status
    assert automation.jobs() == []


def test_installed_revision_is_used_after_manual_git_pull(automation):
    automation.call()
    automation.fake_git(head="b" * 40)
    (automation.state / "installed-revision.json").write_text(json.dumps({"revision": "a" * 40}))
    result = automation.call("check")["result"]
    assert result["status"] == "available"
    assert result["installed_revision_source"] == "installed_metadata"


def test_bridge_install_defers_with_ordinary_job(automation):
    automation.fake_git()
    automation.seed_job(status="running")
    assert automation.call("check", install=True)["result"]["status"] == "deferred_busy"
    assert not list((automation.state / "bridge-updates").glob("*.json"))


def test_manifest_is_strict_and_mutations_require_request_id():
    specs = json.loads((ROOT / "tools/aria_bridge/automation-tools.json").read_text())
    assert len({s["name"] for s in specs}) == len(specs)
    for spec in specs:
        assert spec["name"] == "aria_" + spec["action"]
        if not spec["read_only"]:
            assert "request_id" in spec["required"]


def test_event_archive_verification_binds_exact_completed_archive(automation):
    saved = automation.save(
        action="operations_archive_verify",
        arguments={},
        trigger="event",
        event_action="operations_archive_create",
        use_event_archive=True,
    )
    assert saved["ok"], saved
    automation.seed_job(result={"archive_id": "backup-123", "sha256": "d" * 64})
    automation.advance(61)
    automation.tick()
    queued = [job for job in automation.jobs() if job["status"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["arguments"] == {"archive_id": "backup-123", "expected_sha256": "d" * 64}
    assert queued[0]["automation"]["source_job_id"] == "source-job"
    assert queued[0]["automation"]["chain"] == ["daily-backup"]


def test_event_archive_binding_cannot_target_another_action(automation):
    assert (
        automation.save(
            trigger="event", event_action="operations_archive_create", use_event_archive=True
        )["error"]
        == "invalid request"
    )


def test_fresh_tick_loads_resource_module_and_records_bounded_sampling(automation):
    assert automation.tick()["ok"]
    sample = automation.state / "automation/resource-sample.json"
    assert sample.is_file()
    first = sample.read_text()
    assert json.loads(first)["timestamp"] == automation.now
    automation.advance(60)
    assert automation.tick()["ok"]
    assert sample.read_text() == first


def test_pruned_notifications_do_not_reappear_on_every_tick(automation):
    automation.call()
    for number in range(205):
        job = dict(
            job_id=f"failed-{number:03d}",
            action="script_run",
            status="failed",
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        (automation.state / "jobs" / f"{job['job_id']}.json").write_text(json.dumps(job))
    automation.tick()
    inbox_path = automation.state / "automation/inbox.json"
    first = inbox_path.read_bytes()
    assert len(json.loads(first)) == 200
    automation.tick()
    assert inbox_path.read_bytes() == first


SCHEDULE_EDIT_COMMAND = r"""#!/usr/bin/env python3
import fcntl, json, os
from pathlib import Path
root = Path(os.environ['EDIT_AUTOMATION_STATE'])
marker = root / 'edit-observed'
if not marker.exists():
    # Match the configuration writer's lock and assert expensive checks do not hold it.
    with (root / 'automation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / 'automation/schedules.json'
        config = json.loads(path.read_text())
        ident = os.environ['EDIT_AUTOMATION_ID']
        mutation = os.environ['EDIT_AUTOMATION_KIND']
        if mutation == 'delete':
            del config['schedules'][ident]
        elif mutation == 'disable':
            config['schedules'][ident]['enabled'] = False
        else:
            config['schedules'][ident]['label'] = 'Changed while checking'
        temporary = path.with_suffix('.new')
        temporary.write_text(json.dumps(config))
        temporary.replace(path)
        marker.write_text('edited')
if os.environ.get('EDIT_OUTPUT_CHECKS') == '1':
    print(Path(os.environ['FAKE_CHECKS']).read_text())
else:
    print('{}')
"""


@pytest.mark.parametrize("mutation", ["disable", "delete", "edit"])
def test_bridge_schedule_change_during_ci_check_prevents_submission(automation, tmp_path, mutation):
    automation.fake_git()
    assert automation.save(action="bridge_auto_update", arguments={})["ok"]
    curl = tmp_path / "mutating-curl"
    curl.write_text(SCHEDULE_EDIT_COMMAND)
    curl.chmod(0o700)
    automation.env.update(
        ARIA_AUTOMATION_CURL=str(curl),
        EDIT_AUTOMATION_STATE=str(automation.state),
        EDIT_AUTOMATION_ID="daily-backup",
        EDIT_AUTOMATION_KIND=mutation,
        EDIT_OUTPUT_CHECKS="1",
    )
    automation.advance(61)
    assert automation.tick()["ok"]
    assert (automation.state / "edit-observed").is_file()
    assert not list((automation.state / "bridge-updates").glob("*.json"))
    check = json.loads((automation.state / "automation/bridge-check.json").read_text())
    assert check["ci_verified"]
    assert check["status"] == "deferred_schedule"


@pytest.mark.parametrize("mutation", ["disable", "delete", "edit"])
def test_ordinary_schedule_change_during_sampling_prevents_submission(
    automation, tmp_path, mutation
):
    assert automation.save()["ok"]
    docker = tmp_path / "mutating-docker"
    docker.write_text(SCHEDULE_EDIT_COMMAND)
    docker.chmod(0o700)
    automation.env.update(
        ARIA_AGENT_DOCKER=str(docker),
        EDIT_AUTOMATION_STATE=str(automation.state),
        EDIT_AUTOMATION_ID="daily-backup",
        EDIT_AUTOMATION_KIND=mutation,
    )
    automation.advance(61)
    assert automation.tick()["ok"]
    assert (automation.state / "edit-observed").is_file()
    assert automation.jobs() == []
