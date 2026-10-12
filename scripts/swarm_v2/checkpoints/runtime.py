import json
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.artifacts.base import ArtifactRef, ArtifactStore
from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import model, native, restore

ROOT = "AGENTIHOOKS_CHECKPOINT_ROOT"


def granted(root: Path, registration: Registration) -> ArtifactStore:
    return ArtifactStore(
        LocalBackend(root),
        lambda token: registration if token == registration.grant_id else None,
        registration.grant_id,
    )


def configured(environ: Mapping[str, str]) -> Path | None:
    value = environ.get(ROOT)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise restore.RestoreRefused("checkpoint root must be absolute")
    return path


def worker_store(spec: dict, environ: Mapping[str, str]) -> ArtifactStore:
    root = configured(environ)
    if root is None:
        raise restore.RestoreRefused(restore.STORE_MISSING)
    registration = Registration(**spec["registration"])
    authority = spec["authority"]
    if any(getattr(registration, key) != value for key, value in authority.items()):
        raise restore.RestoreRefused("checkpoint registration does not match launch authority")
    return granted(root, registration)


def version(harness: str, environ: Mapping[str, str]) -> str:
    return subprocess.run(
        [harness, "--version"], env=dict(environ), capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()


def validate_export(attempt: Path, identifier: str, environ: Mapping[str, str]) -> bool:
    if configured(environ) is None:
        return False
    spec = json.loads((attempt / "launch.json").read_text())
    return model.Validator(worker_store(spec, environ)).verify(identifier).authoritative


def restoring(store: ArtifactStore, directive: restore.Directive, paths: restore.Paths) -> restore.Restored:
    restorer = restore.Restorer(native.Qualifier(model.Validator(store)))
    return restorer.restore(directive, paths.worktree, paths.home, paths.cwd, paths.version_text)


def prepare(spec: dict, attempt: Path, environ: Mapping[str, str] = os.environ) -> restore.Restored | None:
    if "restore" not in spec:
        return None
    directive = restore.Directive.parse(spec["restore"])
    if directive.recovery == restore.FRESH:
        return restore.launched(spec)
    store = worker_store(spec, environ)
    record = json.loads((attempt / "execution.json").read_text())
    execution = filesystem.recorded(attempt.resolve(), record)
    harness = spec["harness"]
    home = execution.path("home") / harness / (".claude" if harness == "claude" else ".codex")
    paths = restore.Paths(execution.path("worktree"), home, attempt, version(harness, environ))
    directive = restore.Directive.parse(spec["restore"])
    restored = restoring(store, directive, paths)
    if restored.recovery == restore.RECONSTRUCTED:
        doc = json.loads(store.get_range(store.recorded(directive.checkpoint_id)))
        material = doc["inventory"][model.TRANSCRIPT]
        text = store.get_range(ArtifactRef(**material)).decode()
        target = attempt / "continuation.txt"
        target.write_text("Continue from this recorded conversation. Native resume was unavailable.\\n" + text)
        target.chmod(0o600)
    return restored


def command(attempt: Path, original: tuple[str, ...]) -> tuple[str, ...]:
    path = attempt / "restore.json"
    if not path.exists():
        return original
    restored = json.loads(path.read_text())
    if restored["recovery"] == restore.NATIVE:
        return tuple(restored["command"])
    if restored["recovery"] == restore.RECONSTRUCTED:
        return (*original, (attempt / "continuation.txt").read_text())
    return original


class Checkpoints:
    def __init__(self, root: Path | None, registered: Callable[[str], Registration | None], compatibility: str) -> None:
        self.root, self.registered, self.compatibility = root, registered, compatibility

    def validator(self, execution_id: str) -> model.Validator | None:
        registration = self.registered(execution_id)
        if self.root is None or registration is None:
            return None
        return model.Validator(granted(self.root, registration))

    def list(self, execution_id: str) -> list[dict]:
        validator = self.validator(execution_id)
        if validator is None:
            return []
        store = validator.store
        documents = []
        for key in store.backend.keys(store.scope.key("artifacts") + "/"):
            identifier = Path(key).stem
            verdict = validator.verify(identifier)
            if verdict.authoritative:
                doc = json.loads(store.get_range(store.recorded(identifier)))
                documents.append(doc)
        newest = max((doc["authority"]["task_generation"] for doc in documents), default=0)
        return [
            {
                "checkpoint_id": doc["checkpoint_id"],
                "sequence": doc["checkpoint_sequence"],
                "status": "complete",
                "compatibility": self.compatibility,
                "continuation": doc["continuation"],
                "watermark": doc["transcript_durable_cursor"]["sequence"],
            }
            for doc in documents
            if doc["authority"]["task_generation"] == newest and doc["authority"]["execution_id"] == execution_id
        ]

    def fetch(self, execution_id: str, checkpoint_id: str) -> dict:
        if checkpoint_id not in {entry["checkpoint_id"] for entry in self.list(execution_id)}:
            raise restore.RestoreRefused("checkpoint is not verified for this execution")
        validator = self.validator(execution_id)
        store = validator.store
        return json.loads(store.get_range(store.recorded(checkpoint_id)))
