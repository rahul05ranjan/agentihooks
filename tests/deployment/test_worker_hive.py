import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/worker/start.sh"


def startup(tmp_path, *, joined=False, pre_existing_env=False, wait_s=None):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    cli = tools / "agentihooks"
    cli.write_text('#!/usr/bin/env bash\nset -euo pipefail\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\n')
    cli.chmod(0o755)
    home = tmp_path / "state"
    home.mkdir()
    credential = tmp_path / "member"
    credential.write_text("fixture member credential\n")
    if pre_existing_env:
        (home / "hive.env").write_text("fixture hive env\n")
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "--initial-branch=dev", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "AGENTIHOOKS_HOME": str(home),
        "SWARM_HIVE_ID": "worker-pod",
        "AGENTIHOOKS_HIVE_ID": "worker-member",
        "AGENTIHOOKS_DEPLOYMENT": "remote",
        "AGENTIHOOKS_HIVE_REDIS_URL": "redis://localhost:6379/15",
        "HIVE_REPO_ROOT": str(tmp_path / "repos"),
        "HIVE_REPOSITORIES": json.dumps([{"url": str(repo), "name": "checkout", "ref": "dev"}]),
        "HIVE_CREDENTIAL_FILE": str(credential) if joined else "",
        "COMMAND_LOG": str(log),
    }
    if wait_s is not None:
        env["HIVE_DELIVERY_WAIT_S"] = str(wait_s)
    result = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", home, tmp_path / "repos/checkout"


def test_mounted_member_skips_wait_and_keeps_private_credentials(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=True)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == [
        "init --profile engineer",
        "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1",
        "hive run",
    ]
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr


def test_preexisting_hive_env_proceeds_without_waiting(tmp_path):
    result, commands, _, repo = startup(tmp_path, pre_existing_env=True)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == [
        "init --profile engineer",
        "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1",
        "hive run",
    ]
    assert (repo / ".git").is_dir()


def test_no_credential_delivered_exits_before_running(tmp_path):
    result, commands, _, _ = startup(tmp_path, wait_s=0)
    assert result.returncode == 1
    assert commands == ""
    assert "hive run" not in commands
    assert result.stderr.strip().endswith(
        "worker-hive: no member credential was delivered within 0s; run agentihooks hive add from the UI hive"
    )
