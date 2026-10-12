from dataclasses import replace

import pytest

from scripts.swarm_v2.checkpoints import model, native, restore, runtime
from tests import sv2_ckp03_cases as cases

pytestmark = pytest.mark.unit


def test_the_production_store_uses_the_registered_attempt_scope(tmp_path):
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    granted = runtime.granted(tmp_path / "durable", registration)
    assert granted.scope.execution_id == "e1"
    assert granted.scope.task_id == "t1"
    assert granted.scope.generation == 2
    assert granted.backend.kind == "local"


def test_controller_listing_reads_only_verified_newest_generation(tmp_path):
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", registration)
    qualifier = native.Qualifier(model.Validator(store))
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    captured = qualifier.capture("claude", home, cases.SESSIONS["claude"], cwd, cases.VERSIONS["claude"], stopped=True)
    assert cases.commit(qualifier, captured, "ckpt-1", "claude").authoritative
    listing = runtime.Checkpoints(tmp_path / "durable", lambda execution: registration, "dev")
    rows = listing.list("e1")
    assert rows == [
        {
            "checkpoint_id": "ckpt-1",
            "sequence": 1,
            "status": "complete",
            "compatibility": "dev",
            "continuation": "native",
            "watermark": 41,
        }
    ]
    ref = store.recorded("ckpt-1")
    store.backend.write(store.scope.key("objects", ref.sha256), b"corrupt")
    assert listing.list("e1") == []


def test_replacement_restores_the_registered_checkpoint_before_returning_a_resume_command(tmp_path):
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", registration)
    qualifier = native.Qualifier(model.Validator(store))
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    captured = qualifier.capture("claude", home, cases.SESSIONS["claude"], cwd, cases.VERSIONS["claude"], stopped=True)
    assert cases.commit(qualifier, captured, "ckpt-1", "claude").authoritative
    replacement = runtime.granted(tmp_path / "durable", replace(registration, execution_id="e2", generation=3))
    directive = restore.Directive("ckpt-1", "native", cases.SESSIONS["claude"], 41)
    paths = restore.Paths(tmp_path / "worktree", tmp_path / "fresh", cwd, cases.VERSIONS["claude"])
    result = runtime.restoring(replacement, directive, paths)
    assert result.command == ("claude", "--resume", cases.SESSIONS["claude"])
    assert result.recovery == "native"


def test_a_drain_requires_a_durable_verified_checkpoint(tmp_path, monkeypatch):
    from scripts.swarm_v2 import supervision_runtime
    from tests.test_swarm_v2_supervision_runtime import supervisor

    owner = supervisor.__wrapped__(tmp_path, monkeypatch)
    owner.environment[runtime.ROOT] = str(tmp_path / "durable")
    monkeypatch.setattr(owner, "quiesce", lambda: True)
    monkeypatch.setattr(owner, "wait", lambda: pytest.fail("unbounded wait"))
    monkeypatch.setattr(supervision_runtime, "checkpoint", lambda *args: "unverified")
    monkeypatch.setattr(supervision_runtime.trees, "cleanup", lambda *args: None)
    monkeypatch.setattr(runtime, "validate_export", lambda *args: False)
    result = owner.drain("termination")
    assert result["checkpoint_status"] == "incomplete"
    assert result["checkpoint_id"] is None


def test_a_supervisor_launch_uses_the_restored_native_command(tmp_path, monkeypatch):
    import json

    from scripts.swarm_v2 import supervision_runtime
    from tests.test_swarm_v2_supervision_runtime import supervisor

    owner = supervisor.__wrapped__(tmp_path, monkeypatch)
    (tmp_path / "restore.json").write_text(
        json.dumps({"recovery": "native", "command": ["codex", "resume", "session"]})
    )
    monkeypatch.setattr(owner, "spawn", lambda *args: None)
    monkeypatch.setattr(owner, "ready", lambda *args: True)
    commands = []
    monkeypatch.setattr(
        owner, "herdr", lambda words: commands.append(words) or {"result": {"root_pane": {"pane_id": "p"}}}
    )
    monkeypatch.setattr(supervision_runtime, "native_command", lambda command, *args: command)
    assert owner.start() is None
    assert "codex resume session" in commands[-1][-1]


def test_malformed_directive_is_refused_before_loading_any_store(tmp_path):
    with pytest.raises(restore.RestoreRefused):
        runtime.prepare({"restore": None}, tmp_path, {})
