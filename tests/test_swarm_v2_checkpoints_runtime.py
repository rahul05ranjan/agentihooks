import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

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


def test_an_older_execution_cannot_offer_a_checkpoint_after_a_new_generation_promotes(tmp_path):
    import json

    from scripts.swarm_v2.auth_context import Registration

    old = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", old)
    qualifier = native.Qualifier(model.Validator(store))
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    taken = qualifier.capture("claude", home, cases.SESSIONS["claude"], cwd, cases.VERSIONS["claude"], stopped=True)
    assert cases.commit(qualifier, taken, "ckpt-1", "claude").authoritative
    store.backend.write(
        store.scope.key("pointers", "00000000000000000003", "00000000000000000001.json"),
        json.dumps({"generation": 3, "checkpoint_id": "newer"}).encode(),
    )
    assert runtime.Checkpoints(tmp_path / "durable", lambda execution: old, "dev").list("e1") == []


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


@pytest.mark.parametrize("storage", [{}, {runtime.ROOT: "/fixture"}, {runtime.REMOTE: "true"}])
@pytest.mark.parametrize("verified", [False, True])
def test_a_drain_requires_a_durable_verified_checkpoint(tmp_path, monkeypatch, storage, verified):
    from scripts.swarm_v2 import supervision_runtime
    from tests.test_swarm_v2_supervision_runtime import supervisor

    owner = supervisor.__wrapped__(tmp_path, monkeypatch)
    owner.environment.update(storage)
    monkeypatch.setattr(owner, "quiesce", lambda: True)
    monkeypatch.setattr(owner, "wait", lambda: pytest.fail("unbounded wait"))
    monkeypatch.setattr(supervision_runtime, "checkpoint", lambda *args: "unverified")
    monkeypatch.setattr(supervision_runtime.trees, "cleanup", lambda *args: None)
    calls = []
    monkeypatch.setattr(runtime, "validate_export", lambda *args: calls.append(args) or verified)
    result = owner.drain("termination")
    complete = not storage or verified
    assert result["checkpoint_status"] == ("complete" if complete else "incomplete")
    assert result["checkpoint_id"] == ("unverified" if complete else None)
    assert calls == ([(owner.launch.attempt, "unverified", owner.environment)] if storage else [])


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


@pytest.fixture
def published_attempt(tmp_path, monkeypatch):
    from scripts.swarm_v2.checkpoints import capture
    from tests.test_swarm_v2_checkpoints_capture import portable

    attempt, scope, environ, newest = portable.__wrapped__(tmp_path, monkeypatch)
    capture.publish(attempt, scope, environ, newest)
    return attempt, environ


@pytest.mark.parametrize("recovery", ["native", "reconstructed"])
def test_worker_prepare_materializes_source_and_conversation(published_attempt, tmp_path, recovery):
    from scripts.swarm_v2 import filesystem
    from tests import sv2_ckp02_cases as sources

    original, environ = published_attempt
    attempt = tmp_path / "e2"
    attempt.mkdir()
    record = {"attempt": "e2", "layout": filesystem.mapping(filesystem.load()), "homes": {"claude": "homes/claude"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    sources.git(tmp_path, "clone", "-q", "-b", "dev", str(tmp_path / "remote.git"), str(attempt / "worktrees"))
    spec = json.loads((original / "launch.json").read_text())
    spec["registration"].update(execution_id="e2", generation=3)
    spec["authority"].update(execution_id="e2", generation=3)
    spec["restore"] = {
        "checkpoint_id": "ckp-fixture",
        "recovery": recovery,
        "session_id": cases.SESSIONS["claude"] if recovery == "native" else "",
        "watermark": 41,
    }
    result = runtime.prepare(spec, attempt, environ)
    assert result.recovery == recovery
    assert result.checkpoint_id == "ckp-fixture"
    assert result.watermark == 2
    assert (attempt / "worktrees" / "app.py").read_text() == "print('committed locally')\n"
    assert (attempt / "worktrees" / "notes.md").read_text() == "staged notes\nunstaged line\n"
    assert (attempt / "worktrees" / "draft.md").read_text() == "allowed untracked draft\n"
    if recovery == "native":
        assert result.command == ("claude", "--resume", cases.SESSIONS["claude"])
        restored = attempt / "homes/claude/.claude/projects" / native.encoded(attempt) / f"{result.session_id}.jsonl"
        assert restored.is_file()
        assert not (attempt / "continuation.txt").exists()
    else:
        assert result.command == ()
        store = runtime.worker_store(spec, attempt, environ)
        doc = json.loads(store.get_range(store.recorded("ckp-fixture")))
        from scripts.swarm_v2.artifacts.base import ArtifactRef

        transcript = store.get_range(ArtifactRef(**doc["inventory"][model.TRANSCRIPT])).decode()
        continuation = attempt / "continuation.txt"
        assert (
            continuation.read_text()
            == "Continue from this recorded conversation. Native resume was unavailable.\n" + transcript
        )
        assert continuation.stat().st_mode & 0o777 == 0o600
        (attempt / "restore.json").write_text(json.dumps(asdict(result)))
        assert runtime.command(attempt, ("claude", "--print")) == ("claude", "--print", continuation.read_text())


def test_worker_prepare_requires_granted_project_before_materialization(published_attempt):
    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    spec["registration"]["project_ids"] = ["local:other"]
    spec["restore"] = {
        "checkpoint_id": "ckp-fixture",
        "recovery": "native",
        "session_id": cases.SESSIONS["claude"],
        "watermark": 1,
    }
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.prepare(spec, attempt, environ)
    assert str(caught.value) == "checkpoint project is not granted to this worker"


def test_worker_prepare_refuses_unverified_checkpoint_before_materialization(published_attempt):
    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    spec["restore"] = {
        "checkpoint_id": "missing",
        "recovery": "native",
        "session_id": cases.SESSIONS["claude"],
        "watermark": 1,
    }
    verdict = model.Validator(runtime.worker_store(spec, attempt, environ)).verify("missing")
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.prepare(spec, attempt, environ)
    assert str(caught.value) == verdict.reason


def test_worker_prepare_fresh_requires_no_store_and_preserves_archived_cursor(tmp_path):
    directive = {"checkpoint_id": "", "recovery": "fresh", "session_id": "", "watermark": 41}
    assert runtime.prepare({"restore": directive}, tmp_path, {}) == restore.Restored("fresh", "", 41)
    assert runtime.prepare({}, tmp_path, {}) is None


def test_checkpoint_configuration_names_invalid_root_and_missing_store(tmp_path):
    assert runtime.configured({}) is None
    assert runtime.configured({runtime.ROOT: ""}) is None
    assert runtime.configured({runtime.ROOT: str(tmp_path)}) == tmp_path
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.configured({runtime.ROOT: "relative"})
    assert str(caught.value) == "checkpoint root must be absolute"
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.worker_store({}, tmp_path, {})
    assert str(caught.value) == restore.STORE_MISSING
    assert runtime.validate_export(tmp_path, "missing", {}) is False


def test_worker_store_refuses_mismatched_launch_authority(published_attempt):
    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    spec["authority"]["execution_id"] = "another"
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.worker_store(spec, attempt, environ)
    assert str(caught.value) == "checkpoint registration does not match launch authority"


def test_remote_store_adapter_uses_the_whole_authenticated_launch(tmp_path, monkeypatch):
    calls = []
    backend = object()
    remote = SimpleNamespace(
        controller_backend=lambda environ: calls.append(("controller", environ)) or backend,
        worker_store=lambda spec, attempt, environ: calls.append(("worker", spec, attempt, environ)) or backend,
    )
    monkeypatch.setitem(sys.modules, "scripts.swarm_v2.artifacts.remote", remote)
    environ = {"AGENTIHOOKS_CHECKPOINT_BUCKET": "fixture", runtime.REMOTE: "true"}
    spec = {"registration": {"execution_id": "e1"}, "authority": {"execution_id": "e1"}}
    assert runtime.configured(environ) is backend
    assert runtime.worker_store(spec, tmp_path, environ) is backend
    assert calls == [("controller", environ), ("worker", spec, tmp_path, environ)]


def test_remote_export_validation_uses_the_launch_scope(published_attempt, monkeypatch):
    attempt, local = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, local)
    calls = []
    remote = SimpleNamespace(worker_store=lambda spec, root, environ: calls.append((spec, root, environ)) or store)
    monkeypatch.setitem(sys.modules, "scripts.swarm_v2.artifacts.remote", remote)
    environ = {runtime.REMOTE: "true"}
    assert runtime.validate_export(attempt, "ckp-fixture", environ) is True
    assert calls == [(spec, attempt, environ)]


@pytest.mark.parametrize("recovery", ["fresh", "reconstructed"])
def test_worker_command_keeps_continuity_type_visible(tmp_path, recovery):
    (tmp_path / "restore.json").write_text(json.dumps({"recovery": recovery, "command": []}))
    (tmp_path / "continuation.txt").write_text("Recorded conversation")
    expected = ("claude", "Recorded conversation") if recovery == "reconstructed" else ("claude",)
    assert runtime.command(tmp_path, ("claude",)) == expected


def test_harness_version_probe_is_bounded_and_reads_its_output(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout=" 2.1.0\n"),
    )
    environ = {"PATH": "/fixture/bin"}
    assert runtime.version("claude", environ) == "2.1.0"
    assert calls == [
        (
            (["claude", "--version"],),
            {"env": environ, "capture_output": True, "text": True, "timeout": 10, "check": True},
        )
    ]


def test_malformed_directive_is_refused_before_loading_any_store(tmp_path):
    with pytest.raises(restore.RestoreRefused):
        runtime.prepare({"restore": None}, tmp_path, {})
