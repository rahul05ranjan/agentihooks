import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import capture, model, native, runtime
from scripts.swarm_v2.supervision_protocol import checkpoint
from tests import sv2_ckp02_cases as source_cases
from tests import sv2_ckp03_cases as native_cases

pytestmark = pytest.mark.unit


@pytest.fixture
def portable(tmp_path, monkeypatch):
    attempt = tmp_path / "e1"
    attempt.mkdir()
    worktree = source_cases.worktree(tmp_path)
    worktree.rename(attempt / "worktrees")
    record = {"attempt": "e1", "layout": filesystem.mapping(filesystem.load()), "homes": {"claude": "homes/claude"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    home = native_cases.home_for("claude", tmp_path, attempt)
    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    spec = {
        "harness": "claude",
        "registration": asdict(registration),
        "authority": {"execution_id": "e1", "generation": 2},
        "image_digest": "sha256:" + "c" * 64,
    }
    (attempt / "launch.json").write_text(json.dumps(spec))
    environ = {runtime.ROOT: str(tmp_path / "durable"), "CLAUDE_CONFIG_DIR": str(home)}
    scope = {"incarnation": "fixture", "authority": spec["authority"]}
    newest = home / "projects" / native.encoded(attempt) / f"{native_cases.SESSIONS['claude']}.jsonl"
    monkeypatch.setattr(runtime, "version", lambda *args: native_cases.VERSIONS["claude"])
    return attempt, scope, environ, newest


def test_worker_capture_publishes_and_validates_native_and_source_material(portable):
    attempt, scope, environ, newest = portable
    acknowledgement = capture.publish(attempt, scope, environ, newest)
    identifier = checkpoint(attempt, {**scope, **acknowledgement}, scope)
    assert identifier == "ckp-fixture"
    assert runtime.validate_export(attempt, identifier, environ)
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, environ)
    validator = model.Validator(store)
    assert validator.verify(identifier).continuation == "native"
    manifest = json.loads(store.get_range(store.recorded(identifier)))
    assert manifest["transcript_durable_cursor"]["sequence"] == 2
    assert set(manifest["inventory"]) >= {"native_session", "normalized_transcript", "source_bundle"}


def test_corruption_after_export_is_refused_by_the_supervisor_validator(portable):
    attempt, scope, environ, newest = portable
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, environ)
    manifest = json.loads(store.get_range(store.recorded("ckp-fixture")))
    ref = manifest["inventory"][model.NATIVE_SESSION]
    store.backend.write(store.scope.key("objects", ref["sha256"]), b"corrupt")
    assert runtime.validate_export(attempt, "ckp-fixture", environ) is False


def test_unsupported_native_version_is_published_only_as_context(portable, monkeypatch):
    attempt, scope, environ, newest = portable
    monkeypatch.setattr(runtime, "version", lambda *args: "9.0.0")
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, environ)
    verdict = model.Validator(store).verify("ckp-fixture")
    assert verdict.authoritative
    assert verdict.continuation == "context"


def test_capture_keeps_execution_generation_when_the_task_generation_differs(portable, monkeypatch):
    attempt, scope, environ, newest = portable
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.granted(Path(environ[runtime.ROOT]), replace(Registration(**spec["registration"]), generation=7))
    monkeypatch.setattr(runtime, "worker_store", lambda *args: store)
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    doc = json.loads(store.get_range(store.recorded("ckp-fixture")))
    assert doc["authority"]["task_generation"] == 7
    assert doc["authority"]["execution_generation"] == 2
    assert doc["reconstruction"]["execution_generation"] == 2
    assert model.Validator(store, execution_generation=2).verify("ckp-fixture").authoritative
