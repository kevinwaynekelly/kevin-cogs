"""Real PHP deployment extension tests with filesystem copies and a fake Docker CLI."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tests import test_aria_host_agent as host_tests

PHP = host_tests.PHP
host_agent = host_tests.agent
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "tools/aria_bridge/host-agent.php"
EXTENSION = ROOT / "tools/aria_bridge/host-deployment.php"
RUNNER = r"""
require $argv[1]; require_once $argv[2];
$a = json_decode(stream_get_contents(STDIN), true); ariaInit($a['state']);
try {
 if ($a['mode'] === 'read') $r = ariaDeploymentRead($a['state'], $a['action'], $a['arguments']);
 elseif ($a['mode'] === 'authorize') {$r = ariaDeploymentAuthorize($a['state'], $a['action'], $a['arguments']);}
 elseif ($a['mode'] === 'health') $r = ariaDeploymentHealth($a['state'], $a['arguments']['name']);
 elseif ($a['mode'] === 'deploy') $r = ariaDeployment($a['state'], 'native-deploy', $a['arguments']);
 else $r = ariaDeploymentExecute($a['state'], ['job_id' => $a['job_id'], 'action' => $a['action'], 'arguments' => $a['arguments']]);
 echo ariaJson(['ok' => true, 'result' => $r]);
} catch (Throwable $e) { echo ariaJson(['ok' => false, 'error' => $e->getMessage()]); }
"""

FAKE_DOCKER = r"""#!/usr/bin/env python3
import hashlib, json, os, re, sys
from pathlib import Path
path = Path(os.environ['ARIA_FAKE_DOCKER_STATE'])
s = json.loads(path.read_text()); c = s['containers']; a = sys.argv[1:]
with Path(os.environ['ARIA_FAKE_DOCKER_LOG']).open('a') as f: f.write(json.dumps(a)+'\n')
def save(): path.write_text(json.dumps(s))
def fail(message='operation failed'):
 print(message, file=sys.stderr); sys.exit(1)
def key(name):
 if name in c: return name
 for k,v in c.items():
  if v['Id'] == name: return k
 fail('Error: No such object')
def opt(name, default=None):
 if name in a: return a[a.index(name)+1]
 return next((v.split('=',1)[1] for v in a if v.startswith(name+'=')), default)
if a[:2] == ['image','inspect']:
 print(json.dumps([{'Id':'sha256:old','RepoDigests':['vendor/app@sha256:'+'a'*64],
 'Created':'2026-01-01T00:00:00Z','Architecture':'amd64','Os':'linux',
 'Config':{'Labels':{'org.opencontainers.image.version':'1.2.3','secret':'NO_LEAK'}}}]))
elif a[0] == 'inspect': print(json.dumps([c[key(a[-1])]]))
elif a[0] == 'ps':
 for n,v in c.items():
  print(json.dumps({'ID':v['Id'][:12],'Names':n,'Image':v['Config']['Image'],'State':v['State']['Status'],'Status':v['State']['Status'],'Ports':''}))
elif a[0] == 'create':
 n=opt('--name')
 if n in c or s.get('fail_create'): fail()
 ident=hashlib.sha256(n.encode()).hexdigest(); labels={}; mounts=[]
 for i,v in enumerate(a):
  if v=='--label': k,v=a[i+1].split('=',1); labels[k]=v
  if v=='--mount':
   parts=dict(x.split('=',1) if '=' in x else (x,True) for x in a[i+1].split(','))
   mounts.append({'Type':'bind','Source':parts['src'],'Destination':parts['dst'],'RW':not parts.get('readonly',False)})
 c[n]={'Id':ident,'Name':'/'+n,'Config':{'Image':a[-1],'Labels':labels},'Image':'sha256:new','State':{'Running':False,'Status':'created'},'HostConfig':{'NetworkMode':opt('--network','bridge'),'Privileged':False,'Devices':[],'PortBindings':{}},'Mounts':mounts}
 save(); print(ident)
elif a[0] in ['start','stop','restart']:
 n=key(a[-1]); v=c[n]; v['State']['Running']=a[0]!='stop'; v['State']['Status']='running' if a[0]!='stop' else 'exited'
 if v['Id'] != s.get('original_id') and a[0] != 'stop': v['State']['Health']={'Status':'unhealthy' if s.get('unhealthy_new') else 'healthy'}
 save()
elif a[0] == 'rename':
 n=key(a[1]); c[a[2]]=c.pop(n); c[a[2]]['Name']='/'+a[2]; save()
elif a[0] == 'rm':
 if s.get('fail_remove'): fail()
 del c[key(a[-1])]; save()
elif a[0] in ['pull','network','update']: pass
else: fail()
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def deployment(host_agent, tmp_path):
    agent = host_agent
    docker = Path(agent.env["ARIA_AGENT_DOCKER"])
    docker.write_text(FAKE_DOCKER)
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    source = appdata / "app"
    source.mkdir()
    (source / "db.txt").write_text("important database contents")
    (source / "empty").mkdir()
    agent.env["ARIA_AGENT_APPDATA"] = str(appdata)
    template = agent.templates / "my-app.xml"
    template.write_text(template.read_text().replace("/mnt/user/appdata/app", str(source)))
    agent.appdata = appdata
    agent.source = source
    agent.template = template

    def call(action, mode="read", job_id="deployment-1", **arguments):
        process = subprocess.run(
            [PHP, "-r", RUNNER, str(HOST), str(EXTENSION)],
            input=json.dumps(
                {
                    "state": str(agent.state),
                    "mode": mode,
                    "action": action
                    if mode == "authorize" or action.startswith("deployment_")
                    else "deployment_" + action,
                    "arguments": arguments,
                    "job_id": job_id,
                }
            ),
            capture_output=True,
            text=True,
            env=agent.env,
            timeout=20,
            check=True,
        )
        assert not process.stderr, process.stderr
        return json.loads(process.stdout)

    def update_container(**changes):
        state = json.loads(agent.docker_state.read_text())
        state["containers"]["app"].update(changes)
        agent.docker_state.write_text(json.dumps(state))

    agent.ext = call
    agent.update_container = update_container
    agent.update_container(
        Mounts=[{"Type": "bind", "Source": str(source), "Destination": "/data", "RW": True}]
    )
    return agent


def save_policy(agent, **policy):
    result = agent.ext(
        "container_policy_save", mode="execute", name="app", policy=policy, expected_sha256=""
    )
    assert result["ok"], result
    return result["result"]


def test_template_preview_redacts_and_does_not_write(deployment):
    old = deployment.template.read_text()
    result = deployment.ext(
        "template_preview",
        template="my-app.xml",
        xml=old.replace("stable", "v2"),
        expected_sha256=sha(deployment.template),
    )
    assert result["ok"], result
    assert result["result"]["changes"] == [
        {"field": "Repository", "before": "vendor/app:stable", "after": "vendor/app:v2"}
    ]
    assert "secret&value" not in json.dumps(result)
    assert deployment.template.read_text() == old


def test_preview_stale_hash_rejected(deployment):
    result = deployment.ext(
        "template_preview",
        template="my-app.xml",
        xml=deployment.template.read_text(),
        expected_sha256="0" * 64,
    )
    assert result == {"ok": False, "error": "hash mismatch"}


def test_config_search_hides_secrets_but_finds_paths(deployment):
    assert deployment.ext("config_search", query="secret&value")["result"]["matches"] == []
    result = deployment.ext("config_search", query=str(deployment.source))
    assert len(result["result"]["matches"]) == 1
    assert result["result"]["matches"][0]["field"] == "Config/Path//data"


def test_drift_detects_mount_network_and_image_changes(deployment):
    deployment.update_container(Config={"Image": "other/image:latest"}, Mounts=[])
    result = deployment.ext("config_drift", name="app")["result"]
    assert {row["field"] for row in result["differences"]} == {"image", "mount:/data"}


def test_pin_verified_digest_restore_backup_and_no_deployment(deployment):
    before = deployment.template.read_bytes()
    digest = "vendor/app@sha256:" + "a" * 64
    result = deployment.ext(
        "image_pin",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        digest=digest,
    )
    assert result["ok"], result
    assert digest in deployment.template.read_text()
    assert not result["result"]["deployed"]
    history = deployment.ext("template_history", template="my-app.xml")["result"]["backups"]
    assert history[0]["backup_id"] == "deployment-1"
    restored = deployment.ext(
        "template_restore",
        mode="execute",
        job_id="restore-1",
        template="my-app.xml",
        backup_id="deployment-1",
        expected_sha256=sha(deployment.template),
    )
    assert restored["ok"], restored
    assert deployment.template.read_bytes() == before
    assert not any(row[0] in {"create", "start", "stop"} for row in deployment.commands())


def test_pin_rejects_unverified_digest(deployment):
    result = deployment.ext(
        "image_pin",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        digest="vendor/app@sha256:" + "b" * 64,
    )
    assert result == {"ok": False, "error": "invalid request"}


def test_image_details_exposes_only_allowed_metadata(deployment):
    result = deployment.ext("image_details", name="app")
    assert result["ok"]
    assert result["result"]["metadata"] == {"org.opencontainers.image.version": "1.2.3"}
    assert "NO_LEAK" not in json.dumps(result)


def test_policy_readonly_blocks_template_pin_and_drift_search_respects_reads(deployment):
    save_policy(deployment, allowed_actions=["inspect"])
    result = deployment.ext(
        "image_pin",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        digest="vendor/app@sha256:" + "a" * 64,
    )
    assert result == {"ok": False, "error": "policy denied"}
    assert deployment.ext("config_drift", name="app")["ok"]


def test_policy_optimistic_lock_rejects_stale_writer(deployment):
    save_policy(deployment, hold_updates=True)
    result = deployment.ext(
        "container_policy_save", mode="execute", name="app", policy={}, expected_sha256=""
    )
    assert result == {"ok": False, "error": "hash mismatch"}


def test_health_requirement_and_unhealthy_failure(deployment):
    assert (
        deployment.ext("health", mode="health", name="app")["result"]["application_health_verified"]
        is False
    )
    save_policy(deployment, require_healthcheck=True)
    assert deployment.ext("health", mode="health", name="app") == {
        "ok": False,
        "error": "healthcheck required",
    }
    deployment.update_container(State={"Running": True, "Health": {"Status": "unhealthy"}})
    assert deployment.ext("health", mode="health", name="app") == {
        "ok": False,
        "error": "command failed",
    }
    deployment.update_container(State={"Running": True, "Health": {"Status": "healthy"}})
    assert (
        deployment.ext("health", mode="health", name="app")["result"]["application_health_verified"]
        is True
    )


def test_native_unhealthy_replacement_rolls_back_old_container(deployment):
    deployment.change_state(unhealthy_new=True)
    result = deployment.ext(
        "native",
        mode="deploy",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        pull=False,
        start=True,
    )
    assert result == {"ok": False, "error": "command failed"}
    state = json.loads(deployment.docker_state.read_text())
    assert state["containers"]["app"]["Id"] == deployment.original_id
    assert state["containers"]["app"]["State"]["Running"] is True
    assert list(state["containers"]) == ["app"]


def stack_definition(agent, cycle=False):
    second = agent.templates / "database.xml"
    second.write_text(
        agent.template.read_text().replace("<Name>app</Name>", "<Name>database</Name>")
    )
    return {
        "members": [
            {
                "template": "my-app.xml",
                "expected_sha256": sha(agent.template),
                "depends_on": ["database.xml"],
            },
            {
                "template": "database.xml",
                "expected_sha256": sha(second),
                "depends_on": ["my-app.xml"] if cycle else [],
            },
        ]
    }


def test_stack_dependency_order_and_stale_template_guard(deployment):
    definition = stack_definition(deployment)
    saved = deployment.ext(
        "stack_save", mode="execute", name="media", definition=definition, expected_sha256=""
    )
    assert saved["ok"], saved
    plan = deployment.ext("stack_plan", name="media")["result"]
    assert [member["template"] for member in plan["order"]] == ["database.xml", "my-app.xml"]
    deployment.template.write_text(deployment.template.read_text().replace("stable", "changed"))
    result = deployment.ext(
        "stack_apply",
        mode="execute",
        name="media",
        expected_sha256=saved["result"]["sha256"],
        pull=False,
        start=True,
    )
    assert result == {"ok": False, "error": "hash mismatch"}
    assert not deployment.commands()


def test_stack_cycle_rejected_without_write(deployment):
    result = deployment.ext(
        "stack_save",
        mode="execute",
        name="media",
        definition=stack_definition(deployment, cycle=True),
        expected_sha256="",
    )
    assert result == {"ok": False, "error": "invalid request"}
    assert not (deployment.state / "stacks/media.json").exists()


def test_migration_copies_verifies_preserves_source_and_updates_template(deployment):
    deployment.update_container(State={"Running": False, "Status": "exited"})
    destination = deployment.appdata / "moved"
    result = deployment.ext(
        "appdata_migrate",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        source=str(deployment.source),
        destination=str(destination),
        max_copy_bytes=1024,
    )
    assert result["ok"], result
    assert result["result"]["verification"]["entries"] == 3
    assert (destination / "db.txt").read_bytes() == (deployment.source / "db.txt").read_bytes()
    assert (destination / "empty").is_dir()
    assert str(destination) in deployment.template.read_text()
    assert result["result"]["source_retained"] is True
    assert not result["result"]["deployed"]


@pytest.mark.parametrize("failure", ["running", "budget", "symlink", "outside", "existing"])
def test_migration_rejects_unsafe_or_unbounded_copy(deployment, tmp_path, failure):
    deployment.update_container(
        State={
            "Running": failure == "running",
            "Status": "running" if failure == "running" else "exited",
        }
    )
    destination = deployment.appdata / "moved"
    if failure == "symlink":
        (deployment.source / "escape").symlink_to(tmp_path)
    if failure == "outside":
        destination = tmp_path / "outside"
    if failure == "existing":
        destination.mkdir()
    before = deployment.template.read_bytes()
    result = deployment.ext(
        "appdata_migrate",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        source=str(deployment.source),
        destination=str(destination),
        max_copy_bytes=1 if failure == "budget" else 1024,
    )
    assert not result["ok"], result
    assert deployment.template.read_bytes() == before
    assert not list(deployment.appdata.glob(".aria-copy-*"))


def test_upgrade_clone_isolated_and_cleanup_verified(deployment):
    deployment.update_container(State={"Running": False, "Status": "exited"})
    result = deployment.ext(
        "upgrade_test",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        image="vendor/app:v2",
        max_copy_bytes=1024,
        health_timeout_seconds=1,
    )
    assert result["ok"], result
    assert result["result"]["status"] == "passed"
    assert result["result"]["cleanup_complete"] is True
    create = next(cmd for cmd in deployment.commands() if cmd[0] == "create")
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert all(str(deployment.source) not in item for item in create)
    assert not list((deployment.state / "upgrade-tests").iterdir())
    assert list(json.loads(deployment.docker_state.read_text())["containers"]) == ["app"]
    assert (deployment.source / "db.txt").read_text() == "important database contents"


def test_upgrade_cleanup_failure_retains_copied_mounts(deployment):
    deployment.update_container(State={"Running": False, "Status": "exited"})
    deployment.change_state(fail_remove=True)
    result = deployment.ext(
        "upgrade_test",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        image="vendor/app:v2",
        max_copy_bytes=1024,
        health_timeout_seconds=1,
    )
    assert result["ok"], result
    assert result["result"]["cleanup_complete"] is False
    assert Path(result["result"]["retained_directory"]).is_dir()


def test_upgrade_rejects_privileged_or_extra_commands(deployment):
    deployment.template.write_text(
        deployment.template.read_text().replace(
            "</Container>", "<ExtraParams>--privileged</ExtraParams></Container>"
        )
    )
    result = deployment.ext(
        "upgrade_test",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        image="vendor/app:v2",
        max_copy_bytes=1024,
        health_timeout_seconds=1,
    )
    assert result == {"ok": False, "error": "unsupported template feature"}
    assert not deployment.commands()


def test_preview_reports_secret_change_without_exposing_value(deployment):
    result = deployment.ext(
        "template_preview",
        template="my-app.xml",
        xml=deployment.template.read_text().replace("secret&amp;value", "ROTATED_SECRET"),
        expected_sha256=sha(deployment.template),
    )
    assert result["ok"], result
    assert result["result"]["redacted_values_changed"] == 1
    assert "ROTATED_SECRET" not in json.dumps(result)


def test_hold_updates_blocks_stack_before_any_deployment(deployment):
    definition = stack_definition(deployment)
    saved = deployment.ext(
        "stack_save", mode="execute", name="media", definition=definition, expected_sha256=""
    )
    assert saved["ok"]
    save_policy(deployment, hold_updates=True)
    result = deployment.ext(
        "stack_apply",
        mode="execute",
        name="media",
        expected_sha256=saved["result"]["sha256"],
        pull=False,
        start=True,
    )
    assert result == {"ok": False, "error": "policy denied"}
    assert not deployment.commands()


def test_policy_activity_unknown_blocks_change(deployment):
    save_policy(deployment, require_idle_profiles=["missing.plex"])
    result = deployment.ext(
        "image_pin",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        digest="vendor/app@sha256:" + "a" * 64,
    )
    assert not result["ok"]
    assert not any(cmd[0] in {"stop", "start", "create"} for cmd in deployment.commands())


def test_policy_stopped_dependency_blocks_change(deployment):
    save_policy(deployment, require_stopped_containers=["app"])
    result = deployment.ext(
        "image_pin",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        digest="vendor/app@sha256:" + "a" * 64,
    )
    assert result == {"ok": False, "error": "policy denied"}


def test_other_container_writing_appdata_blocks_migration(deployment):
    state = json.loads(deployment.docker_state.read_text())
    writer = state["containers"]["app"].copy()
    writer["Id"] = "f" * 64
    writer["Name"] = "/writer"
    state["containers"]["writer"] = writer
    state["containers"]["app"]["State"] = {"Running": False, "Status": "exited"}
    deployment.docker_state.write_text(json.dumps(state))
    result = deployment.ext(
        "appdata_migrate",
        mode="execute",
        template="my-app.xml",
        expected_sha256=sha(deployment.template),
        source=str(deployment.source),
        destination=str(deployment.appdata / "moved"),
        max_copy_bytes=1024,
    )
    assert result == {"ok": False, "error": "appdata in use"}


def test_readonly_policy_blocks_typed_container_exec(deployment):
    save_policy(deployment, allowed_actions=["inspect", "logs"])
    result = deployment.ext(
        "operations_container_exec", mode="authorize", name="app", argv=["/usr/bin/id"]
    )
    assert result == {"ok": False, "error": "policy denied"}
    assert not deployment.commands()


def test_container_policy_denies_core_reads_when_not_allowed(deployment):
    save_policy(deployment, allowed_actions=[])
    for action in ("container_inspect", "container_logs"):
        assert deployment.ext(action, mode="authorize", name="app") == {
            "ok": False,
            "error": "policy denied",
        }
    result = deployment.ext("config_search", query=str(deployment.source))
    assert result["result"]["matches"] == []
