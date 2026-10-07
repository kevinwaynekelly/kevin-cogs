"""Exercise extended job outcomes and live progress through the real PHP worker."""

import hashlib
import json
import subprocess
import time

import pytest

from tests import test_aria_host_agent as host_tests

HOST = host_tests.HOST
PHP = host_tests.PHP
host_agent = host_tests.agent

pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")


@pytest.fixture
def agent(tmp_path):
    return host_agent.__wrapped__(tmp_path)


def run_worker(agent):
    return subprocess.Popen(
        [PHP, str(HOST), "work", str(agent.state)],
        env=agent.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def wait_job(path, predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = json.loads(path.read_text())
        if predicate(job):
            return job
        time.sleep(0.03)
    raise AssertionError(json.loads(path.read_text()))


def test_worker_progress_has_no_raw_arguments_or_output(agent):
    script = agent.scripts / "Maintenance job/script"
    script.write_text("#!/bin/bash\nprintf PRIVATE_PROGRESS_TOKEN\nsleep 2\nprintf finished\n")
    job_id = agent.queue(
        "script_run",
        name="Maintenance job",
        expected_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
    )
    path = agent.state / "jobs" / f"{job_id}.json"
    worker = run_worker(agent)
    try:
        running = wait_job(
            path, lambda job: job.get("progress", {}).get("output_bytes_observed", 0) > 0
        )
        assert running["status"] == "running"
        assert "PRIVATE_PROGRESS_TOKEN" not in json.dumps(running["progress"])
        assert "argv" not in running["progress"]
        assert wait_job(path, lambda job: job["status"] != "running")["status"] == "succeeded"
    finally:
        worker.terminate()
        worker.communicate(timeout=3)


def test_worker_does_not_mark_missing_native_vm_operation_successful(agent):
    agent.env["ARIA_DIAG_VIRSH"] = "/nonexistent/virsh"
    result = agent.call(
        request={
            "action": "diagnostics_vm_action",
            "arguments": {"name": "test-vm", "operation": "start", "request_id": "vm-test"},
        }
    )
    assert result["ok"], result
    job_id = result["result"]["job_id"]
    path = agent.state / "jobs" / f"{job_id}.json"
    worker = run_worker(agent)
    try:
        result = wait_job(path, lambda job: job["status"] not in {"queued", "running"})
        assert result["status"] == "failed"
    finally:
        worker.terminate()
        worker.communicate(timeout=3)


def test_cancelled_queue_job_is_never_claimed_or_run(agent):
    job_id = agent.queue("container_action", name="app", action="restart")
    cancellation = agent.request("automation_job_cancel", job_id=job_id, request_id="cancel-test")
    assert cancellation["ok"]
    worker = run_worker(agent)
    try:
        ready = agent.state / "worker-ready.json"
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert ready.exists()
        time.sleep(0.3)
        assert not agent.commands()
        assert (
            json.loads((agent.state / "jobs" / f"{job_id}.json").read_text())["status"]
            == "cancelled"
        )
    finally:
        worker.terminate()
        worker.communicate(timeout=3)
