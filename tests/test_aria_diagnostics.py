"""Real PHP diagnostics with fake native CLIs, not a live Unraid deployment."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "tools/aria_bridge/host-agent.php"
MODULE = ROOT / "tools/aria_bridge/host-diagnostics.php"
PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI unavailable")

RUNNER = r"""
require_once $argv[1]; require_once $argv[2];
require_once dirname($argv[2]).'/host-automation.php';
$in = json_decode(stream_get_contents(STDIN), true);
ariaInit($in['state']);
try {
    $r = $in['queued']
        ? ariaDiagnosticsExecute($in['state'], ['action'=>$in['action'], 'arguments'=>$in['arguments'], 'job_id'=>'test-job'])
        : ariaDiagnosticsRead($in['state'], $in['action'], $in['arguments']);
    echo ariaJson(['ok'=>true, 'result'=>$r]);
} catch(Throwable $e) { echo ariaJson(['ok'=>false, 'error'=>$e->getMessage()]); }
"""

CLI = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ['DIAG_LOG']).open('a') as f:
    f.write(json.dumps([name, *args])+'\n')
fixtures = json.loads(Path(os.environ['DIAG_FIXTURES']).read_text())
key = name
if name == 'docker': key += ':' + args[0]
if name == 'virsh': key += ':' + args[2]
if key == 'virsh:define': Path(os.environ['DIAG_DEFINE']).write_text(Path(args[-1]).read_text())
v = fixtures.get(key, {'output':'', 'exit':0})
if isinstance(v, list):
    v = v.pop(0) if len(v) > 1 else v[0]
    Path(os.environ['DIAG_FIXTURES']).write_text(json.dumps(fixtures))
sys.stdout.write(v['output'])
sys.exit(v.get('exit', 0))
"""


@pytest.fixture
def diag(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    state = tmp_path / "state"
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    shares = tmp_path / "shares"
    shares.mkdir()
    fixtures_path = tmp_path / "fixtures.json"
    log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_DIAG_MNT": str(mnt),
        "ARIA_DIAG_SHARES": str(shares),
        "DIAG_FIXTURES": str(fixtures_path),
        "DIAG_LOG": str(log),
        "DIAG_DEFINE": str(tmp_path / "defined.xml"),
    }
    for name in [
        "df",
        "getent",
        "curl",
        "openssl",
        "virsh",
        "lsblk",
        "smartctl",
        "lspci",
        "lsusb",
        "nvidia-smi",
        "trivy",
        "git",
        "docker",
    ]:
        binary = binaries / name
        binary.write_text(CLI)
        binary.chmod(0o700)
        env["ARIA_DIAG_" + name.upper().replace("-", "_")] = str(binary)
        if name == "docker":
            env["ARIA_AGENT_DOCKER"] = str(binary)
    fixtures = {}

    class Diagnostics:
        def call(self, action, queued=False, **arguments):
            fixtures_path.write_text(json.dumps(fixtures))
            p = subprocess.run(
                [PHP, "-r", RUNNER, str(HOST), str(MODULE)],
                input=json.dumps(
                    {
                        "state": str(state),
                        "queued": queued,
                        "action": "diagnostics_" + action,
                        "arguments": arguments,
                    }
                ),
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
                check=True,
            )
            assert not p.stderr
            return json.loads(p.stdout)

        def calls(self):
            return [json.loads(line) for line in log.read_text().splitlines()]

    d = Diagnostics()
    d.fixtures = fixtures
    d.env = env
    d.state = state
    d.mnt = mnt
    d.shares = shares
    d.tmp = tmp_path
    return d


def test_manifest_has_unique_explicit_queue_contract():
    manifest = json.loads((MODULE.parent / "diagnostics-tools.json").read_text())
    assert len(manifest) == 26
    assert len({t["name"] for t in manifest}) == len(manifest)
    assert len({t["action"] for t in manifest}) == len(manifest)
    for item in manifest:
        assert item["action"].startswith("diagnostics_")
        if not item["read_only"]:
            assert "request_id" in item["required"]


def test_health_projects_job_metadata_and_source_revision(diag):
    diag.call("health")
    (diag.state / "jobs" / "1.json").write_text(
        json.dumps(
            {
                "job_id": "one",
                "status": "failed",
                "action": "script_run",
                "arguments": {"password": "do-not-expose"},
                "result": "do-not-expose",
            }
        )
    )
    diag.fixtures["git"] = {"output": "a" * 40 + "\n"}
    r = diag.call("health")["result"]
    assert r["queue_counts"] == {"failed": 1}
    assert r["source_revision"] == "a" * 40
    assert "do-not-expose" not in json.dumps(r)
    assert r["worker"]["process_exists"] is False


def test_storage_converts_blocks_and_excludes_mount_options(diag):
    diag.fixtures["df"] = {
        "output": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000 900 100 90% /mnt/cache\n"
    }
    mounts = diag.tmp / "mountinfo"
    mounts.write_text("1 0 8:1 / /mnt/cache rw - ext4 /dev/sda1 rw,password=do-not-expose\n")
    diag.env["ARIA_DIAG_MOUNTINFO"] = str(mounts)
    r = diag.call("storage")["result"]
    assert r["filesystems"][0]["used_bytes"] == 921600
    assert r["mounts"][0]["read_only"] is False
    assert "do-not-expose" not in json.dumps(r)


def test_share_settings_allowlist_omits_secrets_and_symlinks(diag):
    (diag.shares / "Media.cfg").write_text(
        'shareExport="e"\nshareSecurity="private"\npassword="secret"\nshareReadList="kevin"\n'
    )
    (diag.shares / "Linked.cfg").symlink_to(diag.shares / "Media.cfg")
    r = diag.call("shares")["result"]
    assert len(r["shares"]) == 1
    assert r["shares"][0]["settings"]["shareSecurity"] == "private"
    assert "password" not in json.dumps(r)


def test_permissions_are_nonrecursive_and_contained(diag):
    app = diag.mnt / "app"
    app.mkdir(mode=0o750)
    r = diag.call("permissions", path=str(app))["result"]
    assert r["mode"] == "0750"
    assert r["recursive"] is False
    assert not diag.call("permissions", path="/etc/passwd")["ok"]
    (diag.mnt / "escape").symlink_to("/etc")
    assert not diag.call("permissions", path=str(diag.mnt / "escape" / "passwd"))["ok"]


def test_container_diagnosis_redacts_actual_environment_and_explains_evidence(diag):
    diag.fixtures["docker:inspect"] = {
        "output": json.dumps(
            [
                {
                    "Id": "id",
                    "Config": {"Env": ["UNUSUAL=superprivate"]},
                    "State": {"OOMKilled": True, "ExitCode": 137, "Error": "superprivate"},
                    "RestartCount": 8,
                    "Mounts": [{"Type": "bind", "Source": "/missing", "Destination": "/app"}],
                }
            ]
        )
    }
    diag.fixtures["docker:logs"] = {"output": "superprivate token=abcd\n"}
    r = diag.call("container", name="app")["result"]
    assert {f["code"] for f in r["findings"]} == {
        "missing_bind_source",
        "out_of_memory",
        "repeated_restarts",
        "nonzero_exit",
    }
    assert "superprivate" not in json.dumps(r)
    assert "abcd" not in json.dumps(r)


def test_resources_persist_samples_without_recollecting_history(diag):
    diag.fixtures["docker:stats"] = {
        "output": json.dumps({"Name": "app", "CPUPerc": "1.5%", "Extra": "omit"}) + "\n"
    }
    assert diag.call("resources")["result"]["containers"]["rows"][0] == {
        "Name": "app",
        "CPUPerc": "1.5%",
    }
    r = diag.call("resource_history", limit=1)["result"]
    assert r["retained_samples"] == 1
    assert len(diag.calls()) == 1
    assert not diag.call("resource_history", limit=0)["ok"]


def test_bad_resource_output_is_not_saved_as_successful_sample(diag):
    diag.fixtures["docker:stats"] = {"output": "daemon failed", "exit": 1}
    assert diag.call("resources")["result"]["containers"]["exit_code"] == 1
    assert diag.call("resource_history")["result"]["retained_samples"] == 0


def test_dns_deduplicates_addresses_and_uses_fixed_argv(diag):
    diag.fixtures["getent"] = {
        "output": "192.0.2.5 STREAM host\n192.0.2.5 DGRAM host\n2001:db8::1 STREAM host\n"
    }
    r = diag.call("network", kind="dns", host="aria.local")["result"]
    assert r["addresses"] == ["192.0.2.5", "2001:db8::1"]
    assert diag.calls()[0] == ["getent", "ahosts", "aria.local"]


@pytest.mark.parametrize(
    "host", ["-option", "x;touch /tmp/pwn", "x\ny", "a..b", "file:///etc/passwd"]
)
def test_network_rejects_invalid_hosts(diag, host):
    assert diag.call("network", kind="dns", host=host) == {"ok": False, "error": "invalid request"}


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "https://user:pass@example.com", "http://a/#fragment", "https://a/\n"],
)
def test_http_rejects_credentials_and_non_http_urls(diag, url):
    assert not diag.call("network", kind="http", url=url)["ok"]


def test_http_returns_only_metrics_and_never_query_credentials(diag):
    diag.fixtures["curl"] = {
        "output": '{"http_code":200,"remote_ip":"192.0.2.5","total_seconds":0.1,"tls_verify_result":0}'
    }
    r = diag.call("network", kind="http", url="https://example.com/?token=secret")["result"]
    assert r["metrics"]["http_code"] == 200
    assert "secret" not in json.dumps(r)
    args = diag.calls()[0]
    assert args[1] == "--disable"
    assert args[args.index("--output") + 1] == "/dev/null"
    assert "--location" not in args


def test_vm_inspection_omits_graphics_password_and_disk_auth(diag):
    diag.fixtures["virsh:dumpxml"] = {
        "output": '<domain><name>Gaming VM</name><uuid>abc</uuid><memory unit="KiB">4096</memory><vcpu>4</vcpu><devices><graphics passwd="secret"/><disk type="file" device="disk"><driver type="qcow2"/><source file="/mnt/vms/gaming.qcow2"/><auth username="secret"/><target dev="vda" bus="virtio"/></disk><interface type="bridge"><source bridge="br0"/><mac address="aa:bb"/></interface></devices></domain>'
    }
    diag.fixtures["virsh:domstate"] = {"output": "running\n"}
    r = diag.call("vm_inspect", name="Gaming VM")["result"]
    assert r["state"] == "running"
    assert r["disks"][0]["format"] == "qcow2"
    assert "secret" not in json.dumps(r)


def test_vm_shutdown_reports_request_acceptance_without_claiming_poweroff(diag):
    diag.fixtures["virsh:shutdown"] = {"output": "Domain shutdown requested"}
    diag.fixtures["virsh:domstate"] = {"output": "running\n"}
    r = diag.call("vm_action", queued=True, name="Gaming VM", operation="shutdown")["result"]
    assert r["request_accepted"] is True
    assert r["observed_state"] == "running"
    assert "asynchronously" in r["note"]
    assert diag.calls()[0] == [
        "virsh",
        "--connect",
        "qemu:///system",
        "shutdown",
        "--domain",
        "Gaming VM",
    ]
    assert not diag.call("vm_action", queued=True, name="Gaming VM", operation="destroy")["ok"]


def test_smart_preserves_health_warning_bitmask_without_hiding_report(diag):
    diag.fixtures["smartctl"] = {
        "exit": 8,
        "output": json.dumps(
            {
                "smart_status": {"passed": False},
                "serial_number": "omit",
                "temperature": {"current": 55},
            }
        ),
    }
    r = diag.call("smart", queued=True, device="/dev/sda")["result"]
    assert r["exit_code"] == 0
    assert r["smartctl_exit_status"] == 8
    assert r["health_warning"] is True
    assert r["data"]["smart_status"]["passed"] is False
    assert "serial_number" not in r["data"]
    assert not diag.call("smart", queued=True, device="/dev/sda;reboot")["ok"]


def test_missing_scanner_is_explicit_and_not_a_clean_scan(diag):
    diag.env["ARIA_DIAG_TRIVY"] = "/does/not/exist"
    assert diag.call("security_capabilities")["result"]["trivy_available"] is False
    r = diag.call("security_scan", queued=True, image="vendor/app:stable")["result"]
    assert r["available"] is False
    assert r["exit_code"] == 127
    assert r["complete"] is False


def test_security_scan_projects_findings_and_uses_literal_template_tabs(diag):
    diag.fixtures["trivy"] = {"output": "CVE-2026-1\tssl\t1.0\t1.1\tHIGH\n"}
    r = diag.call("security_scan", queued=True, image="vendor/app:stable")["result"]
    assert r["complete"] is True
    assert r["findings"][0]["fixed_version"] == "1.1"
    args = diag.calls()[0]
    assert "\t" in args[args.index("--template") + 1]
    assert "--scanners" in args
    assert not diag.call("security_scan", queued=True, image="--output=/tmp/file")["ok"]


def test_security_output_overflow_cannot_be_reported_clean(diag):
    diag.fixtures["trivy"] = {"output": "CVE-2026-1\tssl\t1.0\t1.1\tHIGH\n" * 3000}
    r = diag.call("security_scan", queued=True, image="vendor/app:stable")["result"]
    assert r["truncated"] is True
    assert r["complete"] is False
    assert len(r["findings"]) <= 512


def test_cleanup_plan_does_not_delete_anything(diag):
    diag.fixtures["docker:system"] = {
        "output": json.dumps({"Type": "Images", "Size": "10GB", "Reclaimable": "5GB"}) + "\n"
    }
    r = diag.call("cleanup_plan")["result"]
    assert r["rows"][0]["Reclaimable"] == "5GB"
    assert diag.calls() == [["docker", "system", "df", "--format", "{{json .}}"]]


VM_XML = '<domain type="kvm"><name>Gaming VM</name><uuid>vm-uuid</uuid><memory unit="KiB">4096</memory><vcpu>4</vcpu><devices><graphics passwd="keep-private"/><disk type="file" device="disk"><driver type="qcow2"/><source file="/mnt/vms/gaming.qcow2"/><target dev="vda" bus="virtio"/></disk></devices></domain>'


def vm_setup(diag, xml=VM_XML):
    diag.fixtures["virsh:dumpxml"] = {"output": xml}
    diag.fixtures["virsh:list"] = {"output": "Gaming VM\n"}
    return hashlib.sha256(xml.encode()).hexdigest()


def test_vm_config_save_restores_secret_markers_and_keeps_backup(diag):
    sha = vm_setup(diag)
    current = diag.call("vm_config", name="Gaming VM")["result"]
    assert current["sha256"] == sha
    assert "keep-private" not in current["xml"]
    assert "__ARIA_REDACTED_" in current["xml"]
    edited = current["xml"].replace("<vcpu>4</vcpu>", "<vcpu>6</vcpu>")
    result = diag.call(
        "vm_config_save", queued=True, name="Gaming VM", xml=edited, expected_sha256=sha
    )["result"]
    assert result["configuration_saved"] is True
    saved = Path(diag.env["DIAG_DEFINE"]).read_text()
    assert "<vcpu>6</vcpu>" in saved
    assert "keep-private" in saved
    assert (diag.state / "backups/test-job.vm.xml").read_text() == VM_XML
    assert not (diag.state / "backups/test-job.vm-new.xml").exists()
    assert "keep-private" not in json.dumps(result)


def test_vm_edit_rejects_stale_hash_running_vm_and_identity_change(diag):
    sha = vm_setup(diag)
    assert (
        diag.call(
            "vm_config_save", queued=True, name="Gaming VM", xml=VM_XML, expected_sha256="0" * 64
        )["error"]
        == "hash mismatch"
    )
    assert (
        diag.call(
            "vm_config_save",
            queued=True,
            name="Gaming VM",
            xml=VM_XML.replace("vm-uuid", "other"),
            expected_sha256=sha,
        )["error"]
        == "invalid request"
    )
    diag.fixtures["virsh:list"] = {"output": ""}
    r = diag.call("vm_config_save", queued=True, name="Gaming VM", xml=VM_XML, expected_sha256=sha)[
        "result"
    ]
    assert r["exit_code"] == 1
    assert "shut off" in r["error"]
    assert not any(c[3] == "define" for c in diag.calls() if c[0] == "virsh")


def test_vm_native_validation_errors_cannot_echo_secret_xml(diag):
    sha = vm_setup(diag)
    diag.fixtures["virsh:define"] = {"output": "invalid graphics password keep-private", "exit": 1}
    r = diag.call("vm_config_save", queued=True, name="Gaming VM", xml=VM_XML, expected_sha256=sha)[
        "result"
    ]
    assert r["configuration_saved"] is False
    assert "keep-private" not in json.dumps(r)
    assert (diag.state / "backups/test-job.vm.xml").exists()


def test_vm_xml_entities_are_rejected(diag):
    sha = vm_setup(diag)
    xml = '<!DOCTYPE domain [<!ENTITY read SYSTEM "file:///etc/passwd">]><domain><name>&read;</name></domain>'
    assert (
        diag.call("vm_config_save", queued=True, name="Gaming VM", xml=xml, expected_sha256=sha)[
            "error"
        ]
        == "invalid request"
    )


def test_vm_snapshot_creation_requires_offline_qcow2_and_atomic_native_mode(diag):
    sha = vm_setup(diag)
    r = diag.call(
        "vm_snapshot_create",
        queued=True,
        name="Gaming VM",
        snapshot="before-update",
        expected_sha256=sha,
    )["result"]
    assert r["completed"] is True
    native = next(c for c in diag.calls() if c[0] == "virsh" and c[3] == "snapshot-create-as")
    assert "--atomic" in native
    assert "--disk-only" not in native
    assert "--validate" in native
    raw = VM_XML.replace('type="qcow2"', 'type="raw"')
    sha = vm_setup(diag, raw)
    r = diag.call(
        "vm_snapshot_create",
        queued=True,
        name="Gaming VM",
        snapshot="before-update",
        expected_sha256=sha,
    )["result"]
    assert r["exit_code"] == 1
    assert "qcow2" in r["error"]


def test_vm_snapshot_revert_checks_metadata_hash_and_acknowledgement(diag):
    sha = vm_setup(diag)
    snapshot = (
        '<domainsnapshot><name>before-update</name><state>shutoff</state><creationTime>1700000000</creationTime><disks><disk name="vda" snapshot="internal"/></disks>'
        + VM_XML
        + "</domainsnapshot>"
    )
    diag.fixtures["virsh:snapshot-dumpxml"] = {"output": snapshot}
    metadata = diag.call("vm_snapshot_read", name="Gaming VM", snapshot="before-update")["result"]
    assert metadata["state"] == "shutoff"
    assert "keep-private" not in json.dumps(metadata)
    args = dict(
        name="Gaming VM",
        snapshot="before-update",
        expected_sha256=sha,
        expected_snapshot_sha256=metadata["sha256"],
    )
    assert not diag.call("vm_snapshot_revert", queued=True, **args)["ok"]
    r = diag.call("vm_snapshot_revert", queued=True, discard_current_disk_changes=True, **args)[
        "result"
    ]
    assert r["completed"] is True
    native = next(c for c in diag.calls() if c[0] == "virsh" and c[3] == "snapshot-revert")
    assert native[-2:] == ["Gaming VM", "before-update"]
    assert "--force" not in native


def test_vm_snapshot_revert_refuses_external_or_live_snapshots(diag):
    sha = vm_setup(diag)
    for state, disk in [("running", "internal"), ("shutoff", "external")]:
        snapshot = (
            f'<domainsnapshot><name>old</name><state>{state}</state><disks><disk name="vda" snapshot="{disk}"/></disks>'
            + VM_XML
            + "</domainsnapshot>"
        )
        diag.fixtures["virsh:snapshot-dumpxml"] = {"output": snapshot}
        r = diag.call(
            "vm_snapshot_revert",
            queued=True,
            name="Gaming VM",
            snapshot="old",
            expected_sha256=sha,
            expected_snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
            discard_current_disk_changes=True,
        )["result"]
        assert r["exit_code"] == 1
    assert not any(c[3] == "snapshot-revert" for c in diag.calls() if c[0] == "virsh")


def test_device_assignment_maps_shared_paths_without_exposing_docker_env(diag):
    diag.fixtures["docker:ps"] = {"output": "a" * 64 + "\n" + "b" * 64 + "\n"}
    rows = [
        dict(
            name="/" + name,
            devices=[
                {"PathOnHost": "/dev/dri/renderD128", "PathInContainer": "/dev/dri/renderD128"}
            ],
            device_requests=[],
            mounts=[],
            Env=["secret"],
        )
        for name in ["plex", "jellyfin"]
    ]
    diag.fixtures["docker:inspect"] = {"output": "\n".join(json.dumps(r) for r in rows) + "\n"}
    configs = diag.tmp / "vms"
    configs.mkdir()
    (configs / "gaming.xml").write_text(
        VM_XML.replace(
            "</devices>",
            '<hostdev type="pci"><source><address domain="0x0000" bus="0x01" slot="0x00" function="0x0"/></source></hostdev></devices>',
        )
    )
    diag.env["ARIA_DIAG_VM_CONFIGS"] = str(configs)
    r = diag.call("device_assignments")["result"]
    assert r["shared_container_devices"][0]["owners"] == ["container:plex", "container:jellyfin"]
    assert r["vm_configurations"][0]["devices"][0]["type"] == "pci"
    assert "Env" not in json.dumps(r)
    assert "keep-private" not in json.dumps(r)


def test_vm_edit_detects_concurrent_configuration_change_before_define(diag):
    sha = vm_setup(diag)
    diag.fixtures["virsh:dumpxml"] = [
        {"output": VM_XML},
        {"output": VM_XML.replace("<vcpu>4</vcpu>", "<vcpu>8</vcpu>")},
    ]
    r = diag.call("vm_config_save", queued=True, name="Gaming VM", xml=VM_XML, expected_sha256=sha)
    assert r["error"] == "hash mismatch"
    assert not any(c[3] == "define" for c in diag.calls() if c[0] == "virsh")
    assert (diag.state / "backups/test-job.vm.xml").exists()
    assert not (diag.state / "backups/test-job.vm-new.xml").exists()


def test_vm_edit_detects_vm_started_before_define(diag):
    sha = vm_setup(diag)
    diag.fixtures["virsh:list"] = [{"output": "Gaming VM\n"}, {"output": ""}]
    r = diag.call("vm_config_save", queued=True, name="Gaming VM", xml=VM_XML, expected_sha256=sha)[
        "result"
    ]
    assert r["exit_code"] == 1
    assert "became active" in r["error"]
    assert not any(c[3] == "define" for c in diag.calls() if c[0] == "virsh")


def test_mutating_native_timeouts_report_unknown(diag):
    diag.fixtures["virsh:shutdown"] = {"output": "", "exit": 124}
    r = diag.call("vm_action", queued=True, name="Gaming VM", operation="shutdown")["result"]
    assert r["status"] == "unknown"
    sha = vm_setup(diag)
    diag.fixtures["virsh:snapshot-create-as"] = {"output": "", "exit": 124}
    r = diag.call(
        "vm_snapshot_create", queued=True, name="Gaming VM", snapshot="test", expected_sha256=sha
    )["result"]
    assert r["status"] == "unknown"
    assert r["completed"] is False


def test_malformed_smart_capture_is_a_failed_job_not_a_healthy_disk(diag):
    diag.fixtures["smartctl"] = {"output": "not JSON", "exit": 0}
    r = diag.call("smart", queued=True, device="/dev/sda")["result"]
    assert r["exit_code"] != 0
    assert "error" in r


def monitor_setup(
    diag, used=90, container_state="running", container_status="Up 1 hour (unhealthy)"
):
    diag.fixtures["df"] = {
        "output": f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000 {used * 10} {1000 - used * 10} {used}% /mnt/cache\n"
    }
    diag.fixtures["docker:ps"] = {
        "output": json.dumps({"name": "plex", "state": container_state, "status": container_status})
        + "\n"
    }


def test_monitor_alerts_are_local_deduplicated_and_reopen_after_recovery(diag):
    monitor_setup(diag)
    first = diag.call("monitor")["result"]
    assert first["status"] == "succeeded"
    assert first["active_alerts"] == 2
    assert first["notifications_created"] == 2
    assert first["automatic_repairs"] is False
    assert diag.call("monitor")["result"]["notifications_created"] == 0
    monitor_setup(diag, used=95)
    critical = diag.call("monitor")["result"]
    assert critical["critical_alerts"] == 1
    assert critical["notifications_created"] == 1
    monitor_setup(diag, used=80, container_status="Up 1 hour (healthy)")
    assert diag.call("monitor")["result"]["active_alerts"] == 0
    monitor_setup(diag, used=95)
    assert diag.call("monitor")["result"]["notifications_created"] == 2
    inbox = json.loads((diag.state / "automation/inbox.json").read_text())
    assert len(inbox) == 5
    assert {n["kind"] for n in inbox} == {"monitor_warning", "monitor_critical"}
    assert all(c[0] in {"df", "docker"} for c in diag.calls())
    assert all(c[1] == "ps" for c in diag.calls() if c[0] == "docker")
    storage = diag.call("history", kind="storage")["result"]
    assert len(storage["samples"]) == 5
    assert storage["samples"][-1]["filesystems"][0]["used_percent"] == 95
    assert len(diag.call("history", kind="monitor")["result"]["samples"]) == 5


def test_failed_monitoring_does_not_mark_existing_alerts_recovered(diag):
    monitor_setup(diag, container_state="restarting", container_status="Restarting (1)")
    assert diag.call("monitor")["result"]["active_alerts"] == 2
    diag.fixtures["df"] = {"output": "failure", "exit": 1}
    diag.fixtures["docker:ps"] = {"output": "failure", "exit": 1}
    failed = diag.call("monitor")["result"]
    assert failed["status"] == "failed"
    assert failed["active_alerts"] == 2
    assert failed["notifications_created"] == 0
    assert len(json.loads((diag.state / "automation/inbox.json").read_text())) == 2


def test_monitor_ignores_readonly_images_and_duplicate_overlay_mounts(diag):
    diag.fixtures["df"] = {
        "output": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/loop0 100 100 0 100% /mnt/disc\noverlay 1000 950 50 95% /var/lib/docker/overlay2/abc/merged\ntmpfs 100 92 8 92% /var/log\n"
    }
    diag.fixtures["docker:ps"] = {"output": ""}
    mounts = diag.tmp / "mounts"
    mounts.write_text("1 0 8:1 / /mnt/disc ro - iso9660 /dev/loop0 ro\n")
    diag.env["ARIA_DIAG_MOUNTINFO"] = str(mounts)
    r = diag.call("monitor")["result"]
    assert r["active_alerts"] == 1
    inbox = json.loads((diag.state / "automation/inbox.json").read_text())
    assert inbox[0]["metadata"]["mountpoint"] == "/var/log"
    assert inbox[0]["kind"] == "monitor_warning"


def test_monitor_bounds_notifications_and_delivers_remaining_incidents_next_poll(diag):
    monitor_setup(diag, used=80)
    diag.fixtures["docker:ps"] = {
        "output": "\n".join(
            json.dumps({"name": f"app-{i}", "state": "running", "status": "Up (unhealthy)"})
            for i in range(40)
        )
        + "\n"
    }
    first = diag.call("monitor")["result"]
    assert first["notifications_created"] == 32
    assert first["pending_notifications"] == 8
    second = diag.call("monitor")["result"]
    assert second["notifications_created"] == 8
    assert second["pending_notifications"] == 0
    assert len(json.loads((diag.state / "automation/inbox.json").read_text())) == 40


def test_explicit_smart_jobs_record_temperature_and_error_counter_trends(diag):
    for temperature, errors in [(40, 0), (45, 2)]:
        diag.fixtures["smartctl"] = {
            "output": json.dumps(
                {
                    "smart_status": {"passed": True},
                    "temperature": {"current": temperature},
                    "power_on_time": {"hours": 500},
                    "ata_smart_attributes": {
                        "table": [
                            {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": errors}},
                            {"id": 250, "raw": {"value": 999}},
                        ]
                    },
                }
            )
        }
        assert diag.call("smart", queued=True, device="/dev/sda")["result"]["exit_code"] == 0
    history = diag.call("history", kind="smart", device="/dev/sda")["result"]
    assert [r["temperature"]["current"] for r in history["samples"]] == [40, 45]
    assert [r["ata_attributes"][0]["raw"]["value"] for r in history["samples"]] == [0, 2]
    assert len(history["samples"][0]["ata_attributes"]) == 1
    assert diag.call("history", kind="smart", device="/dev/sdb")["result"]["samples"] == []
    assert len(diag.calls()) == 2
    assert not diag.call("history", kind="storage", device="/dev/sda")["ok"]


def test_storage_history_retention_is_bounded_and_malformed_capture_is_failure(diag):
    diag.call("history", kind="storage")
    history = diag.state / "diagnostics/storage-history.json"
    history.write_text(json.dumps([{"sequence": i} for i in range(288)]))
    monitor_setup(diag, used=75)
    diag.call("storage")
    retained = json.loads(history.read_text())
    assert len(retained) == 288
    assert retained[0]["sequence"] == 1
    assert retained[-1]["filesystems"][0]["used_percent"] == 75
    diag.fixtures["df"] = {"output": "not a filesystem table"}
    assert diag.call("storage")["result"]["exit_code"] != 0
