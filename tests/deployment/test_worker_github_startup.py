import os
from pathlib import Path

from tests.deployment.test_worker_hive import startup


def test_live_worker_start_mints_before_bundle_clone_without_saving_the_app_credentials(tmp_path, monkeypatch):
    fake = tmp_path / "network"
    fake.mkdir()
    marker = tmp_path / "minted"
    (fake / "sitecustomize.py").write_text(
        "import os\nfrom pathlib import Path\nfrom types import SimpleNamespace\nimport jwt, requests\n"
        "jwt.encode = lambda *a, **k: 'fixture'\n"
        "def post(*a, **k):\n"
        "    Path(os.environ['APP_PROOF_PATH']).touch()\n"
        "    return SimpleNamespace(status_code=201, json=lambda: {'token': 'fixture', 'expires_at': '2099-01-01T00:00:00Z'})\n"
        "requests.post = post\n"
    )
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(fake), str(root), os.environ.get("PYTHONPATH", ""))))
    monkeypatch.setenv("APP_PROOF_PATH", str(marker))
    monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    result, _, home, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert (repo.parent / "agentihooks-bundle/.git").is_dir()
    for path in [home / "hive-container.env", tmp_path / "home/.gitconfig"]:
        text = path.read_text()
        assert "pem" not in text
        assert "fixture" not in text if path.name == ".gitconfig" else "GITHUB_APP_PRIVATE_KEY" not in text
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in (tmp_path / "home/.gitconfig").read_text()


def test_worker_token_refresh_uses_scheduled_thirty_minute_renewals(tmp_path, monkeypatch):
    import subprocess
    import sys

    import pytest

    from scripts import github_app_cli
    from scripts.scheduler import process_timer

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    target = tmp_path / "github-token"
    target.write_text("initial")
    target.chmod(0o600)
    waits = []
    calls = []

    class Finished(BaseException):
        pass

    def wait(delay):
        waits.append(delay)
        if len(waits) == 3:
            raise Finished
        assert target.read_text() == "initial"
        assert target.stat().st_mode & 0o777 == 0o600

    def mint(command, *, stdout):
        calls.append(command)
        assert target.read_text() == "initial"
        assert stdout.name == str(tmp_path / "github-token.next")
        assert (tmp_path / "github-token.next").stat().st_mode & 0o777 == 0o600
        stdout.write("renewed")
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0)

    monkeypatch.setattr(process_timer, "wait", wait)
    monkeypatch.setattr(subprocess, "run", mint)
    with pytest.raises(Finished):
        github_app_cli.main(["refresh-file"])
    assert waits == [1800, 1800, 1800]
    assert calls == [[sys.executable, "-m", "scripts.github_app_cli", "token"]] * 2
    assert target.read_text() == "renewed"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "github-token.next").exists()
