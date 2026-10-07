"""Verify the host snapshot writer's atomic replacement and data projection contract."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

EXPORTER = Path(__file__).resolve().parents[1] / "tools/aria_bridge/export-containers.sh"


@pytest.mark.parametrize("fails", [False, True])
def test_container_snapshot_keeps_previous_on_docker_failure(tmp_path, fails):
    if not shutil.which("jq") or not shutil.which("timeout"):
        pytest.skip("The Unraid host snapshot writer requires jq and timeout")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' \'{"Names":["red-discordbot"],"Image":"red:latest",'
        '"State":"running","Status":"Up 3 hours"}\'\n' + ("exit 7\n" if fails else "")
    )
    docker.chmod(0o755)
    appdata = tmp_path / "appdata"
    status = appdata / "status"
    status.mkdir(parents=True)
    snapshot = status / "containers.json"
    snapshot.write_text('{"previous": true}')
    result = subprocess.run(
        ["bash", str(EXPORTER)],
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "ARIA_APPDATA_ROOT": str(appdata),
        },
        capture_output=True,
        timeout=10,
        check=False,
    )
    data = json.loads(snapshot.read_text())
    if fails:
        assert result.returncode != 0
        assert data == {"previous": True}
    else:
        assert result.returncode == 0, result.stderr
        assert set(data) == {"generated_at", "containers"}
        assert data["generated_at"] > 0
        assert data["containers"][0]["Names"] == ["red-discordbot"]
        assert snapshot.stat().st_mode & 0o777 == 0o644
    assert list(status.iterdir()) == [snapshot]
