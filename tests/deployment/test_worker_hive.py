import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/worker/start.sh"
TOKEN = "fixture-github-token-value"


def startup(tmp_path, *, joined=False, refused=False, bundle=False, deps_exit=0):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    cli = tools / "agentihooks"
    cli.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$*" == "hive join "* ]]; then\n'
        '  [[ "$REFUSE_JOIN" == no ]] || exit 9\n'
        '  touch "$AGENTIHOOKS_HOME/hive.env"\nfi\n'
        'if [[ "$*" == "deps ensure" ]]; then exit "$DEPS_EXIT"; fi\n'
    )
    cli.chmod(0o755)
    home = tmp_path / "state"
    home.mkdir()
    credential = tmp_path / "member"
    credential.write_text("fixture member credential\n")
    code = tmp_path / "code"
    code.write_text("fixture-code\n")
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
        "HOME": str(tmp_path / "home"),
        "GITHUB_PERSONAL_ACCESS_TOKEN": TOKEN,
        "HIVE_BUNDLE_URL": str(repo) if bundle else "",
        "HIVE_BUNDLE_REF": "dev",
        "DEPS_EXIT": str(deps_exit),
        "AGENTIHOOKS_HOME": str(home),
        "SWARM_HIVE_ID": "worker-pod",
        "AGENTIHOOKS_HIVE_ID": "worker-member",
        "AGENTIHOOKS_DEPLOYMENT": "remote",
        "AGENTIHOOKS_HIVE_REDIS_URL": "redis://localhost:6379/15",
        "HIVE_REPO_ROOT": str(tmp_path / "repos"),
        "HIVE_REPOSITORIES": json.dumps([{"url": str(repo), "name": "checkout", "ref": "dev"}]),
        "HIVE_JOIN_URL": "https://join.example.test",
        "HIVE_JOIN_CODE_FILE": str(code),
        "HIVE_CREDENTIAL_FILE": str(credential) if joined else "",
        "COMMAND_LOG": str(log),
        "REFUSE_JOIN": "yes" if refused else "no",
    }
    result = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", home, tmp_path / "repos/checkout"


def test_code_join_clones_repo_and_configures_before_run(tmp_path):
    result, commands, home, repo = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == [
        "hive join https://join.example.test fixture-code",
        "init --profile engineer",
        "deps ensure",
        "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1",
        "hive run",
    ]
    assert (repo / ".git").is_dir()
    assert (home / "hive.env").exists()
    container_env = home / "hive-container.env"
    assert container_env.stat().st_mode & 0o777 == 0o600
    assert "AGENTIHOOKS_HIVE_ID=worker-member\n" in container_env.read_text()
    assert "COMMAND_LOG=" not in container_env.read_text()


def test_mounted_member_skips_join_and_keeps_private_credentials(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=True)
    assert result.returncode == 0, result.stderr
    assert "hive join" not in commands
    assert "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1\n" in commands
    assert commands.endswith("hive run\n")
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr


def test_refused_join_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refused=True)
    assert result.returncode == 9
    assert "hive run" not in commands
    assert "hive set" not in commands


def test_bundle_is_cloned_beside_the_repos_and_linked_by_init(tmp_path):
    result, commands, _, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    bundle = repo.parent / "agentihooks-bundle"
    assert (bundle / ".git").is_dir()
    branch = subprocess.run(["git", "-C", str(bundle), "branch", "--show-current"], capture_output=True, text=True)
    assert branch.stdout == "dev\n"
    assert commands.splitlines()[1:3] == [f"init --bundle {bundle} --profile engineer", "deps ensure"]
    assert commands.endswith("hive run\n")


def test_github_token_reaches_git_by_reference_and_is_never_written(tmp_path):
    result, _, home, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    config = (tmp_path / "home/.gitconfig").read_text()
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in config
    written = [path for root in (tmp_path / "home", home, repo.parent) for path in root.rglob("*") if path.is_file()]
    assert not [path for path in written if TOKEN.encode() in path.read_bytes()]
    assert TOKEN not in result.stdout + result.stderr
    fill = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        env={**os.environ, "HOME": str(tmp_path / "home"), "GITHUB_PERSONAL_ACCESS_TOKEN": TOKEN},
        capture_output=True,
        text=True,
    )
    assert fill.returncode == 0, fill.stderr
    assert f"username=x-access-token\npassword={TOKEN}\n" in fill.stdout


def test_failed_deps_ensure_still_runs_the_hive(tmp_path):
    result, commands, _, _ = startup(tmp_path, bundle=True, deps_exit=4)
    assert result.returncode == 0, result.stderr
    assert "worker-hive: agentihooks deps ensure failed" in result.stderr
    assert commands.endswith("hive run\n")
