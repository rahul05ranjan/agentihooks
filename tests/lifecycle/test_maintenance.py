import pytest

from hooks.lifecycle import maintenance
from hooks.lifecycle.run import sweep
from scripts import gc_cli

from .conftest import git, snap


@pytest.fixture(autouse=True)
def _global_config(monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _registered() -> list[str]:
    return maintenance.registered_repos()


def _objects(path) -> dict:
    counts = dict(line.split(": ", 1) for line in git(path, "count-objects", "-v").splitlines())
    return {"loose": int(counts["count"]), "packs": int(counts["packs"])}


def test_an_enforcing_sweep_registers_a_checkout_lacking_maintenance_and_reports_its_objects(repo, tmp_path):
    repo.worktree("held")
    assert str(repo.primary.resolve()) not in _registered()
    report = sweep([repo.root()], snap(), tmp_path / "state", act=True)
    assert str(repo.primary.resolve()) in _registered()
    assert report["git_maintenance"] == [
        {"path": str(repo.primary), "registered": True, "outcome": "registered", **_objects(repo.primary)}
    ]
    again = sweep([repo.root()], snap(), tmp_path / "state", act=True)
    assert again["git_maintenance"][0]["outcome"] == ""
    assert _registered().count(str(repo.primary.resolve())) == 1


def test_a_report_only_sweep_counts_objects_without_registering(repo, tmp_path):
    repo.worktree("held")
    report = sweep([repo.root()], snap(), tmp_path / "state")
    entry = report["git_maintenance"][0]
    assert entry["registered"] is False and entry["outcome"] == ""
    assert entry["loose"] == _objects(repo.primary)["loose"] > 0
    assert str(repo.primary.resolve()) not in _registered()


def test_lifecycle_setup_registers_every_managed_primary(repo):
    repo.worktree("held")
    lines = maintenance.setup([repo.root()])
    assert str(repo.primary.resolve()) in _registered()
    assert lines == [f"[OK] git maintenance registered for {repo.primary}"]
    assert maintenance.setup([repo.root()]) == []


def test_gc_prints_the_object_backlog_per_checkout(monkeypatch, capsys):
    report = {
        "totals": {},
        "findings": [],
        "git_maintenance": [{"path": "/r/a", "registered": False, "outcome": "", "loose": 78532, "packs": 47}],
    }
    monkeypatch.setattr(gc_cli, "sweep", lambda scope="", act=False: report)
    assert gc_cli.main(["gc"]) == 0
    assert "git       78532 loose    47 packs  /r/a  (maintenance not registered)" in capsys.readouterr().out
