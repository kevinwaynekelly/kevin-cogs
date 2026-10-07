"""Saved host authentication stays outside Git/API command arguments and MCP results."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PHP = shutil.which("php")
HOST = Path(__file__).resolve().parents[1] / "tools/aria_bridge/host-agent.php"
pytestmark = pytest.mark.skipif(PHP is None, reason="PHP CLI is unavailable")


def run_php(tmp_path, code, credentials="", cli=True):
    settings = tmp_path / "credentials.conf"
    settings.write_text(credentials)
    settings.chmod(0o600)
    executable = tmp_path / "fake gh's cli"
    executable.write_text(
        "#!/usr/bin/env python3\nimport os,sys,json\n"
        "if sys.argv[1:3] == ['auth','git-credential']:\n"
        " print('username=bridge\\npassword='+os.environ['GH_TOKEN']); sys.exit(0)\n"
        "print(json.dumps({'private_authenticated':os.environ.get('GH_TOKEN') == 'test-token', "
        "'config':os.environ.get('GH_CONFIG_DIR'), 'arguments':sys.argv[1:]}))\n"
    )
    executable.chmod(0o700)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}
    }
    env.update(
        ARIA_AGENT_TEST_MODE="1",
        ARIA_GITHUB_CREDENTIALS=str(settings),
        ARIA_GITHUB_CLI=str(executable) if cli else "",
    )
    result = subprocess.run(
        [
            PHP,
            "-r",
            "require $argv[1]; try { "
            + code
            + " } catch (Throwable $e) { echo json_encode(['error'=>$e->getMessage()]); }",
            str(HOST),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_private_checks_use_saved_token_without_secret_arguments(tmp_path):
    result = run_php(
        tmp_path,
        "echo ariaGithubChecks('kevinwaynekelly/aria-gpt-bridge', str_repeat('a',40))['output'];",
        "GITHUB_TOKEN='test-token'\nGH_CONFIG_DIR=\"/mnt/user/saved gh\"\n",
    )
    assert result["private_authenticated"] is True
    assert result["config"] == "/mnt/user/saved gh"
    assert "test-token" not in str(result["arguments"])
    assert result["arguments"] == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "repos/kevinwaynekelly/aria-gpt-bridge/commits/" + "a" * 40 + "/check-runs?per_page=100",
    ]


def test_git_helper_quotes_cli_path_and_keeps_token_in_environment(tmp_path):
    result = run_php(
        tmp_path,
        "$a=ariaGithubAuthentication(); echo json_encode(['options'=>ariaGithubGitOptions($a), 'saved'=>$a['environment']['GH_TOKEN']==='test-token']);",
        "export GITHUB_TOKEN=test-token # saved login\n",
    )
    assert result["saved"] is True
    assert "test-token" not in str(result["options"])
    assert "'\\''" in result["options"][-1]
    assert result["options"][-1].endswith(" auth git-credential")


def test_real_git_can_use_cli_path_with_apostrophe(tmp_path):
    result = run_php(
        tmp_path,
        "$a=ariaGithubAuthentication(); $p=[]; $s=proc_open(array_merge(['/usr/bin/git'],ariaGithubGitOptions($a),['credential','fill']),[0=>['pipe','r'],1=>['pipe','w'],2=>['pipe','w']],$p,null,$a['environment'],['bypass_shell'=>true]); fwrite($p[0],\"protocol=https\\nhost=github.com\\n\\n\"); fclose($p[0]); $out=stream_get_contents($p[1]); fclose($p[1]); $err=stream_get_contents($p[2]); fclose($p[2]); echo json_encode(['exit'=>proc_close($s),'authenticated'=>strpos($out,'password=test-token')!==false,'error_empty'=>$err==='']);",
        "GITHUB_TOKEN=test-token",
    )
    assert result == {"exit": 0, "authenticated": True, "error_empty": True}


@pytest.mark.parametrize("assignment", ["GITHUB_TOKEN=$(touch SENTINEL)", 'GITHUB_TOKEN="$HOME"'])
def test_credentials_are_never_evaluated_as_shell_code(tmp_path, assignment):
    result = run_php(tmp_path, "echo json_encode(ariaGithubAuthentication());", assignment)
    assert result["error"] == "GitHub settings must use literal assignments"
    assert not (tmp_path / "SENTINEL").exists()


def test_saved_token_requires_cli(tmp_path):
    result = run_php(
        tmp_path,
        "echo json_encode(ariaGithubAuthentication());",
        "GITHUB_TOKEN=test-token",
        cli=False,
    )
    assert result["error"] == "GitHub CLI is required for saved token authentication"


def test_credentials_with_group_or_other_access_are_rejected(tmp_path):
    result = run_php(
        tmp_path,
        "chmod(getenv('ARIA_GITHUB_CREDENTIALS'),0644); echo json_encode(ariaGithubAuthentication());",
        "GITHUB_TOKEN=test-token",
    )
    assert result["error"] == "GitHub credentials file must be root-private"


def test_relative_cli_configuration_directory_is_rejected(tmp_path):
    result = run_php(
        tmp_path,
        "echo json_encode(ariaGithubAuthentication());",
        "GH_CONFIG_DIR=relative",
    )
    assert result["error"] == "GitHub configuration directory must be absolute"


def test_checks_reject_untrusted_destination_before_authentication(tmp_path):
    result = run_php(
        tmp_path,
        "echo json_encode(ariaGithubChecks('other/repo', str_repeat('a',40)));",
    )
    assert result["error"] == "invalid request"


def test_git_without_cli_preserves_existing_credential_helpers(tmp_path):
    result = run_php(
        tmp_path,
        "echo json_encode(ariaGithubGitOptions(ariaGithubAuthentication()));",
        cli=False,
    )
    assert result == ["-c", "credential.interactive=false"]
