"""Exercise the PHP host boundary with real PHP/bash and a stateful fake Docker CLI.

These tests do not use a Docker daemon or an Unraid server. Direct PHP dispatch
avoids requiring a Unix socket in restricted CI environments.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1] / "tools/aria_bridge/host-agent.php"
PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")

XML = """<Container version="2"><Name>app</Name><Repository>vendor/app:stable</Repository>
<Network>bridge</Network><Config Name="API key" Target="API_KEY" Type="Variable"
Default="fallback-token" Mask="true">secret&amp;value</Config>
<Config Name="Data" Target="/data" Type="Path" Mode="rw">/mnt/user/appdata/app</Config>
</Container>"""

PHP_RUNNER = r"""
require $argv[1];
$a = json_decode(stream_get_contents(STDIN), true);
ariaInit($a['state']);
try {
    if ($a['mode'] === 'dispatch') $r = ariaDispatch($a['state'], $a['request']);
    elseif ($a['mode'] === 'execute') {
        $job = ariaReadJson($a['state'].'/jobs/'.$a['job_id'].'.json');
        $r = ariaExecute($a['state'], $job);
    } elseif ($a['mode'] === 'prune') { ariaPrune($a['state']); $r = []; }
    elseif ($a['mode'] === 'run') $r = ariaRun($a['argv'], $a['seconds'], $a['cwd']);
    echo ariaJson(['ok' => true, 'result' => $r]);
} catch (Throwable $e) { echo ariaJson(['ok' => false, 'error' => ariaError($e)]); }
"""

FAKE_DOCKER = r"""#!/usr/bin/env python3
import hashlib, json, os, re, sys
from pathlib import Path
path = Path(os.environ['ARIA_FAKE_DOCKER_STATE'])
state = json.loads(path.read_text())
args = sys.argv[1:]
with Path(os.environ['ARIA_FAKE_DOCKER_LOG']).open('a') as out:
    out.write(json.dumps(args) + '\n')
containers = state['containers']
def save(): path.write_text(json.dumps(state))
def missing():
    print('Error: No such object', file=sys.stderr)
    sys.exit(1)
def fail():
    print('operation failed with SECRET_THAT_MUST_NOT_LEAK', file=sys.stderr)
    sys.exit(1)
def info(name, running, generation):
    return {'Id': hashlib.sha256(str(generation).encode()).hexdigest(),
            'Name': '/' + name, 'Config': {'Image': 'vendor/app:stable', 'Env': ['API_KEY=secret&value']},
            'Image': 'sha256:123', 'State': {'Running': running, 'Status': 'running' if running else 'exited'},
            'Mounts': [], 'NetworkSettings': {'Networks': {'bridge': {}}, 'Ports': {}},
            'HostConfig': {'RestartPolicy': {'Name': 'always'}, 'Privileged': False, 'NetworkMode': 'bridge'}}
action = args[0]
if state.get('daemon_failure'): fail()
if action == 'inspect':
    name = args[-1]
    if name not in containers: missing()
    print(json.dumps([containers[name]]))
elif action == 'ps':
    template = args[args.index('--format') + 1]
    for name, item in containers.items():
        row = {'ID': item['Id'][:12], 'Names': name, 'Image': item['Config']['Image'],
               'State': item['State']['Status'], 'Status': item['State']['Status'], 'Ports': '',
               **item.get('PsFields', {})}
        if template == '{{json .}}': print(json.dumps(row))
        else: print(re.sub(r'{{json \.(\w+)}}', lambda m: json.dumps(row[m[1]]), template))
elif action == 'pull':
    if state.get('fail_pull'): fail()
    if state.get('replace_during_pull'):
        containers['app'] = info('app', True, 999)
        save()
    print('pulled')
elif action == 'create':
    name = next(a.split('=', 1)[1] for a in args if a.startswith('--name='))
    if name in containers or state.get('fail_create'): fail()
    state['generation'] = state.get('generation', 10) + 1
    containers[name] = info(name, False, state['generation'])
    print(containers[name]['Id']); save()
elif action in {'stop', 'start', 'restart'}:
    name = args[-1]
    if name not in containers: missing()
    if action == 'start' and state.get('fail_start') and containers[name]['Id'] != state.get('original_id'): fail()
    containers[name]['State']['Running'] = action != 'stop' and not state.get('start_exits')
    containers[name]['State']['Status'] = 'exited' if action == 'stop' or state.get('start_exits') else 'running'
    save(); print(name)
elif action == 'rename':
    old, new = args[1:]
    if old not in containers or new in containers: fail()
    containers[new] = containers.pop(old); containers[new]['Name'] = '/' + new
    save()
elif action == 'rm':
    name = args[-1]
    if name not in containers: missing()
    del containers[name]; save()
elif action == 'network':
    _, _, network, name = args
    if network in containers[name]['NetworkSettings']['Networks']: fail()
    containers[name]['NetworkSettings']['Networks'][network] = {}; save()
elif action == 'logs': print('connected with secret&value and token=other-secret')
elif action == 'update': pass
else: fail()
"""


@pytest.fixture
def agent(tmp_path):
    if subprocess.run(
        [PHP, "-r", "exit(function_exists('simplexml_load_string')?0:1);"], check=False
    ).returncode:
        pytest.skip("PHP SimpleXML extension is unavailable")
    state = tmp_path / "state"
    templates = tmp_path / "templates"
    scripts = tmp_path / "scripts"
    templates.mkdir()
    scripts.mkdir()
    (templates / "my-app.xml").write_text(XML)
    script_dir = scripts / "Maintenance job"
    script_dir.mkdir()
    (script_dir / "script").write_text('#!/bin/bash\nprintf "ran in %s\\n" "$PWD"\n')
    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o700)
    native = tmp_path / "native.php"
    native.write_text(
        "<?php\nfunction xmlToVar($xml) {$x=simplexml_load_string($xml); $n=(string)$x->Network ?: (string)$x->Networking->Mode; return ['Network'=>$n==='missing'?'none':$n];}\nfunction xmlToCommand($xml, $paths=false) {\n"
        "$x=simplexml_load_string($xml); $cmd=escapeshellarg(getenv('ARIA_AGENT_DOCKER'));\n"
        "return [$cmd.' create '.escapeshellarg('--name='.(string)$x->Name).' '."
        "escapeshellarg((string)$x->Repository), (string)$x->Name, (string)$x->Repository];}\n"
    )
    # The real native helper's absolute executable is unquoted. Keep the fixture
    # format identical while the pytest path is safe and contains no whitespace.
    native.write_text(
        native.read_text().replace(
            "$cmd=escapeshellarg(getenv('ARIA_AGENT_DOCKER'));", "$cmd=getenv('ARIA_AGENT_DOCKER');"
        )
    )
    docker_state = tmp_path / "docker-state.json"
    docker_log = tmp_path / "docker-log.jsonl"
    original_id = hashlib.sha256(b"original").hexdigest()
    initial = {
        "original_id": original_id,
        "containers": {
            "app": {
                "Id": original_id,
                "Name": "/app",
                "Config": {"Image": "vendor/app:stable", "Env": ["API_KEY=secret&value"]},
                "Image": "sha256:old",
                "State": {"Running": True, "Status": "running"},
                "Mounts": [],
                "NetworkSettings": {"Networks": {"bridge": {}}, "Ports": {}},
                "HostConfig": {
                    "RestartPolicy": {"Name": "always"},
                    "Privileged": False,
                    "NetworkMode": "bridge",
                },
            }
        },
    }
    docker_state.write_text(json.dumps(initial))
    env = {
        **os.environ,
        "ARIA_AGENT_TEST_MODE": "1",
        "ARIA_AGENT_TEMPLATES": str(templates),
        "ARIA_AGENT_SCRIPTS": str(scripts),
        "ARIA_AGENT_DOCKER": str(docker),
        "ARIA_AGENT_NATIVE": str(native),
        "ARIA_FAKE_DOCKER_STATE": str(docker_state),
        "ARIA_FAKE_DOCKER_LOG": str(docker_log),
    }

    class Agent:
        def call(self, mode="dispatch", **kwargs):
            proc = subprocess.run(
                [PHP, "-d", "short_open_tag=On", "-r", PHP_RUNNER, str(HOST)],
                input=json.dumps({"state": str(state), "mode": mode, **kwargs}),
                text=True,
                capture_output=True,
                env=env,
                timeout=15,
                check=True,
            )
            return json.loads(proc.stdout)

        def request(self, operation, **args):
            return self.call(request={"action": operation, "arguments": args})

        def queue(self, operation, **args):
            result = self.request(operation, request_id=args.pop("request_id", "request-1"), **args)
            assert result["ok"], result
            return result["result"]["job_id"]

        def execute(self, job_id):
            return self.call(mode="execute", job_id=job_id)

        def change_state(self, **flags):
            data = json.loads(docker_state.read_text())
            data.update(flags)
            docker_state.write_text(json.dumps(data))

        def commands(self):
            return (
                [json.loads(line) for line in docker_log.read_text().splitlines()]
                if docker_log.exists()
                else []
            )

    instance = Agent()
    instance.state = state
    instance.templates = templates
    instance.scripts = scripts
    instance.env = env
    instance.docker_state = docker_state
    instance.original_id = original_id
    return instance


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("status", ["Up 2 hours (healthy)", 'Up "quoted"\\value\nnext line'])
def test_container_listing_omits_large_unused_fields_before_capture(agent, status):
    original = json.loads(agent.docker_state.read_text())["containers"]["app"]
    containers = {
        f"app-{number}": {
            **original,
            "Id": hashlib.sha256(str(number).encode()).hexdigest(),
            "PsFields": {"Labels": "private-label=" + "x" * 1200, "Status": status},
        }
        for number in range(66)
    }
    agent.change_state(containers=containers)
    full = subprocess.run(
        [agent.env["ARIA_AGENT_DOCKER"], "ps", "-a", "--format", "{{json .}}"],
        env=agent.env,
        capture_output=True,
        check=True,
    )
    assert len(full.stdout) > 65536
    response = agent.request("containers")
    assert response["ok"], response
    rows = response["result"]["containers"]
    assert {row["Names"] for row in rows} == set(containers)
    assert len(rows) == 66
    for row in rows:
        item = containers[row["Names"]]
        assert row == {
            "ID": item["Id"][:12],
            "Names": row["Names"],
            "Image": original["Config"]["Image"],
            "State": "running",
            "Status": status,
            "Ports": "",
        }


def test_container_listing_rejects_truncated_projected_output(agent):
    containers = json.loads(agent.docker_state.read_text())["containers"]
    containers["app"]["PsFields"] = {"Status": "x" * 70000}
    agent.change_state(containers=containers)
    assert agent.request("containers") == {"ok": False, "error": "command failed"}


def test_template_redaction_roundtrip_preserves_ampersands_and_backup(agent):
    original = (agent.templates / "my-app.xml").read_text()
    response = agent.request("template_get", template="my-app.xml")["result"]
    assert "secret&amp;value" not in response["xml"]
    assert "fallback-token" not in response["xml"]
    job = agent.queue(
        "template_save",
        template="my-app.xml",
        xml=response["xml"].replace("vendor/app:stable", "vendor/app:new"),
        expected_sha256=response["sha256"],
    )
    assert agent.execute(job)["ok"]
    result = (agent.templates / "my-app.xml").read_text()
    assert "secret&amp;value" in result
    assert 'Default="fallback-token"' in result
    assert "vendor/app:new" in result
    assert (agent.state / "backups" / f"{job}.xml").read_text() == original


def test_save_hash_conflict_and_create_only_never_overwrite(agent):
    before = (agent.templates / "my-app.xml").read_bytes()
    for index, expected in enumerate(["", "f" * 64]):
        job = agent.queue(
            "template_save",
            request_id=f"r{index}",
            template="my-app.xml",
            xml=XML,
            expected_sha256=expected,
        )
        response = agent.execute(job)
        assert response["error"] == ("template exists" if not expected else "hash mismatch")
    assert (agent.templates / "my-app.xml").read_bytes() == before


def test_unknown_redaction_placeholder_cannot_be_saved(agent):
    job = agent.queue(
        "template_save",
        template="new.xml",
        xml=XML.replace("secret&amp;value", "__ARIA_REDACTED_deadbeef__"),
        expected_sha256="",
    )
    assert agent.execute(job) == {"ok": False, "error": "invalid template"}
    assert not (agent.templates / "new.xml").exists()


@pytest.mark.parametrize(
    "name", ["../evil.xml", "/etc/shadow", "-bad.xml", "good.xml\n", "sub/file.xml"]
)
def test_template_traversal_and_option_injection_rejected(agent, name):
    assert agent.request("template_get", template=name) == {"ok": False, "error": "invalid request"}


def test_symlink_template_and_script_rejected(agent, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text(XML)
    (agent.templates / "linked.xml").symlink_to(outside)
    (agent.scripts / "linked").symlink_to(
        agent.scripts / "Maintenance job", target_is_directory=True
    )
    assert agent.request("template_get", template="linked.xml")["error"] == "not found"
    assert agent.request("script_get", name="linked")["error"] == "not found"


def test_deduplication_is_durable_and_private(agent):
    first = agent.request(
        "container_action", name="app", action="restart", request_id="stable-request"
    )
    duplicate = agent.request(
        "container_action", name="app", action="restart", request_id="stable-request"
    )
    assert duplicate["result"]["job_id"] == first["result"]["job_id"]
    assert duplicate["result"]["deduplicated"] is True
    assert (
        agent.request("container_action", name="app", action="stop", request_id="stable-request")[
            "error"
        ]
        == "request_id conflict"
    )
    status = agent.request("job_status", job_id=first["result"]["job_id"])["result"]
    assert status["status"] == "queued"
    assert "arguments" not in status
    assert agent.commands() == []


@pytest.mark.parametrize("failure", ["fail_create", "fail_start"])
def test_failed_replace_rolls_back_original_running_container(agent, failure):
    agent.change_state(**{failure: True})
    job = agent.queue(
        "template_deploy",
        template="my-app.xml",
        expected_sha256=sha(agent.templates / "my-app.xml"),
    )
    assert agent.execute(job) == {"ok": False, "error": "command failed"}
    current = json.loads(agent.docker_state.read_text())["containers"]
    assert list(current) == ["app"]
    assert current["app"]["Id"] == agent.original_id
    assert current["app"]["State"]["Running"] is True
    assert all("-v" not in command for command in agent.commands())


def test_update_preserves_stopped_state_and_tag(agent):
    data = json.loads(agent.docker_state.read_text())
    data["containers"]["app"]["State"] = {"Running": False, "Status": "exited"}
    agent.change_state(containers=data["containers"])
    job = agent.queue("container_update", name="app")
    result = agent.execute(job)
    assert result["ok"], result
    assert result["result"]["status"] == "created"
    commands = agent.commands()
    assert ["pull", "vendor/app:stable"] in commands
    assert not any(command[0] == "start" for command in commands)
    assert all("-v" not in command for command in commands)


def test_unknown_anonymous_volume_is_rejected_before_stop(agent):
    data = json.loads(agent.docker_state.read_text())
    data["containers"]["app"]["Mounts"] = [
        {"Type": "volume", "Name": "anonymous123", "Destination": "/data"}
    ]
    agent.change_state(containers=data["containers"])
    job = agent.queue("container_update", name="app")
    assert agent.execute(job)["error"] == "unsupported template feature"
    assert not any(command[0] in {"stop", "rename", "pull"} for command in agent.commands())


def test_tailscale_template_is_rejected_before_stop(agent):
    path = agent.templates / "my-app.xml"
    path.write_text(
        XML.replace("</Container>", "<TailscaleEnabled>true</TailscaleEnabled></Container>")
    )
    job = agent.queue("container_update", name="app")
    assert agent.execute(job)["error"] == "unsupported template feature"
    assert not agent.commands()


def test_unknown_native_network_is_rejected_before_stop(agent):
    path = agent.templates / "my-app.xml"
    path.write_text(XML.replace("<Network>bridge</Network>", "<Network>missing</Network>"))
    job = agent.queue("container_update", name="app")
    assert agent.execute(job)["error"] == "unsupported template feature"
    assert not agent.commands()


def test_legacy_template_network_fallback_is_supported(agent):
    path = agent.templates / "my-app.xml"
    path.write_text(
        XML.replace("<Network>bridge</Network>", "<Networking><Mode>bridge</Mode></Networking>")
    )
    job = agent.queue("container_update", name="app")
    assert agent.execute(job)["ok"]


def test_start_exit_zero_but_container_exited_reports_failure(agent):
    agent.change_state(start_exits=True)
    job = agent.queue("container_action", name="app", action="start")
    assert agent.execute(job)["error"] == "command failed"


def test_extra_network_duplicate_is_applied_once(agent):
    path = agent.templates / "my-app.xml"
    path.write_text(
        XML.replace(
            "</Container>", "<ExtraNetworks>bridge,extras,extras</ExtraNetworks></Container>"
        )
    )
    job = agent.queue("container_update", name="app")
    assert agent.execute(job)["ok"]
    assert sum(command[0] == "network" for command in agent.commands()) == 1


def test_container_changed_while_pulling_is_not_touched(agent):
    agent.change_state(replace_during_pull=True)
    job = agent.queue("container_update", name="app")
    assert agent.execute(job)["error"] == "hash mismatch"
    assert not any(command[0] in {"stop", "rename", "rm"} for command in agent.commands())


def test_daemon_error_is_not_mistaken_for_missing_container(agent):
    agent.change_state(daemon_failure=True)
    job = agent.queue(
        "template_deploy",
        template="my-app.xml",
        expected_sha256=sha(agent.templates / "my-app.xml"),
    )
    assert agent.execute(job)["error"] == "command failed"
    assert not any(command[0] in {"stop", "rename", "create", "rm"} for command in agent.commands())


def test_full_script_read_and_reviewed_hash_execution(agent):
    path = agent.scripts / "Maintenance job" / "script"
    path.write_text("#!/bin/bash\n" + "# comment\n" * 9000 + 'printf "executed\\n"\n')
    reviewed = agent.request("script_get", name="Maintenance job")["result"]
    assert reviewed["truncated"] is False
    assert reviewed["script"].startswith("#!/bin/bash")
    job = agent.queue("script_run", name="Maintenance job", expected_sha256=reviewed["sha256"])
    assert agent.execute(job)["result"]["output"] == "executed\n"
    path.write_text("echo changed")
    assert agent.execute(job)["error"] == "hash mismatch"


def test_script_timeout_kills_descendants_and_output_is_bounded(agent):
    path = agent.scripts / "Maintenance job" / "script"
    path.write_text(
        "#!/bin/bash\nwhile true; do echo alive >> heartbeat; sleep 0.05; done &\necho $! > child.pid\nwait\n"
    )
    job = agent.queue(
        "script_run", name="Maintenance job", expected_sha256=sha(path), timeout_seconds=1
    )
    response = agent.execute(job)["result"]
    assert response["timed_out"] is True
    heartbeat = path.parent / "heartbeat"
    count = heartbeat.stat().st_size
    time.sleep(0.2)
    assert heartbeat.stat().st_size == count
    output = agent.call(
        mode="run",
        argv=["/bin/bash", "-c", "head -c 200000 /dev/zero | tr '\\0' a"],
        seconds=5,
        cwd=str(path.parent),
    )["result"]
    assert output["truncated"] is True
    assert len(output["output"]) == 65536


def test_logs_and_inspect_do_not_expose_environment_secrets(agent):
    inspected = agent.request("container_inspect", name="app")
    logs = agent.request("container_logs", name="app")
    assert "secret&value" not in json.dumps(inspected)
    assert "secret&value" not in json.dumps(logs)
    assert "other-secret" not in json.dumps(logs)


def test_protected_bridge_but_explicit_infrastructure_changes_allowed(agent):
    bridge = agent.queue("container_action", name="aria-gpt-bridge", action="restart")
    assert agent.execute(bridge)["error"] == "protected container"
    data = json.loads(agent.docker_state.read_text())
    data["containers"]["haproxy"] = {**data["containers"]["app"], "Name": "/haproxy"}
    agent.change_state(containers=data["containers"])
    proxy = agent.queue("container_action", request_id="proxy", name="haproxy", action="restart")
    assert agent.execute(proxy)["ok"]
    bulk = agent.queue("containers_update_all", request_id="bulk")
    plan = json.loads((agent.state / "jobs" / f"{bulk}.json").read_text())["arguments"]["plan"]
    assert next(item for item in plan if item["name"] == "haproxy")["skip"] == "protected container"


def test_worker_restart_marks_running_unknown_without_replay(agent):
    job_id = agent.queue("container_action", name="app", action="restart")
    path = agent.state / "jobs" / f"{job_id}.json"
    job = json.loads(path.read_text())
    job["status"] = "running"
    path.write_text(json.dumps(job))
    worker = subprocess.Popen(
        [PHP, str(HOST), "work", str(agent.state)],
        env=agent.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and json.loads(path.read_text())["status"] != "unknown":
            time.sleep(0.02)
        assert json.loads(path.read_text())["status"] == "unknown"
        assert not agent.commands()
    finally:
        worker.terminate()
        worker.communicate(timeout=3)


def test_job_pruning_keeps_receipts_for_deduplication(agent):
    job_id = agent.queue("container_action", name="app", action="restart")
    job_path = agent.state / "jobs" / f"{job_id}.json"
    existing = json.loads(job_path.read_text())
    existing["status"] = "succeeded"
    job_path.write_text(json.dumps(existing))
    for number in range(520):
        (agent.state / "jobs" / f"z{number:04}.json").write_text(
            json.dumps({**existing, "job_id": f"z{number:04}"})
        )
    assert agent.call(mode="prune")["ok"]
    assert len(list((agent.state / "jobs").glob("*.json"))) == 512
    replay = agent.request("container_action", name="app", action="restart", request_id="request-1")
    assert replay["result"] == {"job_id": job_id, "status": "expired", "deduplicated": True}
