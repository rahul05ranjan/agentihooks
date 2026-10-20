import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/worker/start.sh"


def startup(tmp_path, *, joined=True, delivered=False, wait_s=None, init_fails=False, ssh=False, sshd_dies=False):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    sshd = tools / "sshd"
    sshd.write_text(
        '#!/usr/bin/env bash\nprintf "sshd %s\\n" "$*" >> "$COMMAND_LOG"\n[[ "$1" != -D ]] || [[ "$SSHD_DIES" == yes ]] || exec sleep 5 >/dev/null 2>&1\n'
    )
    sshd.chmod(0o755)
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "authorized_keys").write_text("ssh-ed25519 fixture operator\n")
    (keys / "host_key").write_text("fixture host key\n")
    cli = tools / "agentihooks"
    cli.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$1" == init ]]; then [[ "$REFUSE_INIT" == no ]] || exit 7; fi\n'
    )
    cli.chmod(0o755)
    home = tmp_path / "state"
    home.mkdir()
    credential = tmp_path / "member"
    credential.write_text("fixture member credential\n")
    if delivered:
        (home / "hive.env").write_text("fixture delivered hive env\n")
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
        "REFUSE_INIT": "yes" if init_fails else "no",
        "AH_CC_TOKEN_fixture": "fixture $token (value)",
        "AH_CC_TOKEN_quoted": "it's quoted",
        "AH_CC_TOKEN_escaped": "back\\\\slash",
        "SWARM_SECRET": "fixture tick secret",
        "SSHD_DIES": "yes" if sshd_dies else "no",
    }
    if wait_s is not None:
        env["HIVE_DELIVERY_WAIT_S"] = str(wait_s)
    if ssh:
        env |= {
            "HIVE_SSHD": str(sshd),
            "HIVE_SSH_AUTHORIZED_KEYS_FILE": str(keys / "authorized_keys"),
            "HIVE_SSH_HOST_KEY_FILE": str(keys / "host_key"),
        }
    result = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", home, tmp_path / "repos/checkout"


RUN = [
    "init --profile engineer",
    "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1",
    "hive run",
]


def test_mounted_member_clones_repo_and_configures_before_run(tmp_path):
    result, commands, home, repo = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == RUN
    assert (repo / ".git").is_dir()
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr
    container_env = home / "hive-container.env"
    assert container_env.stat().st_mode & 0o777 == 0o600
    assert "AGENTIHOOKS_HIVE_ID=worker-member\n" in container_env.read_text()
    assert "COMMAND_LOG=" not in container_env.read_text()
    sourced = subprocess.run(
        ["bash", "-c", 'set -a; . "$1"; printf %s "$AH_CC_TOKEN_fixture"', "_", str(container_env)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sourced.stdout == "fixture $token (value)"
    assert "AH_CC_TOKEN_quoted" not in container_env.read_text()
    assert "AH_CC_TOKEN_quoted holds a quote or backslash" in result.stderr
    assert "AH_CC_TOKEN_escaped=" not in container_env.read_text()
    assert "SWARM_SECRET=" not in container_env.read_text()


def test_a_delivered_hive_env_runs_without_waiting(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=False, delivered=True, wait_s=0)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == RUN
    assert (home / "hive.env").read_text() == "fixture delivered hive env\n"


def test_no_delivered_credential_by_the_deadline_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, joined=False, wait_s=0)
    assert result.returncode == 1
    assert commands == ""
    assert result.stderr.splitlines()[-1] == (
        "worker-hive: no member credential was delivered within 0s; run agentihooks hive add from the UI hive"
    )


def test_ssh_serves_the_mounted_keys_before_waiting_for_a_delivered_credential(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=False, wait_s=0, ssh=True)
    assert result.returncode == 1
    state = home / "ssh"
    assert commands.splitlines() == [f"sshd -t -f {state}/sshd_config", f"sshd -D -e -f {state}/sshd_config"]


def test_ssh_checks_its_config_then_serves_the_mounted_keys_before_init(tmp_path):
    result, commands, home, _ = startup(tmp_path, ssh=True)
    assert result.returncode == 0, result.stderr
    state = home / "ssh"
    config = (state / "sshd_config").read_text()
    assert commands.splitlines()[0:3] == [
        f"sshd -t -f {state}/sshd_config",
        f"sshd -D -e -f {state}/sshd_config",
        "init --profile engineer",
    ]
    assert (state / "host_key").stat().st_mode & 0o777 == 0o600
    assert f"AuthorizedKeysFile {tmp_path}/keys/authorized_keys\n" in config
    assert "Port 2222\n" in config
    assert "AllowTcpForwarding no\n" in config
    assert "PasswordAuthentication no\n" in config


def test_an_sshd_that_dies_at_start_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, ssh=True, sshd_dies=True)
    assert result.returncode != 0
    assert "init --profile engineer" not in commands


def test_failed_profile_init_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, init_fails=True)
    assert result.returncode == 7
    assert commands.splitlines()[-1] == "init --profile engineer"
