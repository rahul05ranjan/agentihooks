from dataclasses import replace

import pytest

from scripts.swarm_v2.checkpoints import model, native, restore, runtime
from tests import sv2_ckp03_cases as cases

pytestmark = pytest.mark.unit


def test_the_production_store_uses_the_registered_attempt_scope(tmp_path):
    fixture = cases.store(tmp_path / "fixture")
    registration = fixture._registration if hasattr(fixture, "_registration") else None
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
