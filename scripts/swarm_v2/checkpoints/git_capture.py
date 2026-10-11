"""Source checkpoints: committed, staged, unstaged and permitted untracked work, captured into granted storage."""

import io
import json
import subprocess
import tarfile
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from hooks import secrets
from hooks.context.credential_guard import sensitive_kind
from scripts.swarm_v2.artifacts.base import ArtifactRef
from scripts.swarm_v2.checkpoints import model
from scripts.swarm_v2.filesystem import LayoutError, contain

METRIC = "checkpoint_source_bytes"
STAGED = "staged_patch"
UNSTAGED = "unstaged_patch"
UNTRACKED = "untracked_archive"
PUSHED = "pushed"
BUNDLED = "bundled"
CACHES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "node_modules"})
CACHE_SUFFIXES = (".pyc", ".pyo")
EXECUTABLE = 0o755
DIFF = (
    "--binary",
    "--full-index",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
ESCAPE = "the untracked archive holds a member that is not a regular file inside the worktree"
DIRTY = "the restore target has changes of its own"


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    file_limit: int = 1 << 20
    total_limit: int = 16 << 20

    def describe(self) -> list[str]:
        return [
            "untracked files git ignores",
            "cache directories: " + ", ".join(sorted(CACHES)),
            "compiled files: " + ", ".join(CACHE_SUFFIXES),
            "credential files by name",
            "untracked files whose content matches a secret pattern",
            "links and other files that are not regular",
            f"untracked files over {self.file_limit} bytes",
            f"untracked files past {self.total_limit} bytes in total",
        ]


@dataclass(frozen=True)
class SourceCapture:
    base_commit: str
    head_commit: str
    branch_name: str
    materials: dict[str, bytes]
    exclusions: tuple[str, ...]

    @property
    def branch_state(self) -> str:
        return BUNDLED if model.SOURCE_BUNDLE in self.materials else PUSHED

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: {role: len(data) for role, data in self.materials.items()}}

    def fields(self) -> dict:
        inventory = {role: asdict(ArtifactRef.of(data)) for role, data in self.materials.items()}
        return {
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "branch_name": self.branch_name,
            "branch_state": self.branch_state,
            "inventory": inventory,
            "artifact_hashes": {role: "sha256:" + ref["sha256"] for role, ref in inventory.items()},
            "exclusions": list(self.exclusions),
        }


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, check=False)
    if proc.returncode:
        raise SourceError(f"git {args[0]} failed with exit {proc.returncode}")
    return proc.stdout


def _line(root: Path, *args: str) -> str:
    return _git(root, *args).decode().strip()


def _refusal(name: str, path: Path, policy: Policy) -> str | None:
    if CACHES.intersection(name.split("/")) or name.endswith(CACHE_SUFFIXES):
        return "cache"
    if sensitive_kind(name):
        return "credential"
    if path.is_symlink() or not path.is_file():
        return "link"
    if path.stat().st_size > policy.file_limit:
        return "size"
    return None


def _member(name: str, path: Path, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    if path.stat().st_mode & 0o100:
        info.mode = EXECUTABLE
    return info


def _archive(root: Path, policy: Policy) -> tuple[bytes, Counter[str]]:
    listed = _git(root, "ls-files", "--others", "--exclude-standard", "-z").decode()
    excluded: Counter[str] = Counter()
    total = 0
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name in sorted(filter(None, listed.split("\0"))):
            path = root / name
            kind = _refusal(name, path, policy)
            data = b"" if kind else path.read_bytes()
            if kind is None and secrets.scan(data.decode(errors="replace"), mode="strict"):
                kind = "secret"
            if kind is None and total + len(data) > policy.total_limit:
                kind = "total"
            if kind:
                excluded[kind] += 1
                continue
            total += len(data)
            archive.addfile(_member(name, path, len(data)), io.BytesIO(data))
    return buffer.getvalue(), excluded


def _bundle(root: Path, base: str) -> bytes:
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "source.bundle"
        _git(root, "bundle", "create", str(target), "HEAD", f"^{base}")
        return target.read_bytes()


def capture(worktree: Path, remote_ref: str, policy: Policy = Policy()) -> SourceCapture:
    """The base is the merge base of HEAD with a ref the remote already holds; commits past it travel as a bundle."""
    root = Path(_line(worktree, "rev-parse", "--show-toplevel"))
    head = _line(root, "rev-parse", "--verify", "HEAD^{commit}")
    base = _line(root, "merge-base", head, remote_ref)
    branch = _line(root, "symbolic-ref", "--short", "HEAD")
    archive, excluded = _archive(root, policy)
    materials = {STAGED: _git(root, "diff", "--cached", *DIFF), UNSTAGED: _git(root, "diff", *DIFF), UNTRACKED: archive}
    if head != base:
        materials[model.SOURCE_BUNDLE] = _bundle(root, base)
    counted = tuple(f"excluded {kind}: {count}" for kind, count in sorted(excluded.items()))
    return SourceCapture(base, head, branch, materials, (*policy.describe(), *counted))


def merged(manifest: dict, source: SourceCapture) -> dict:
    fields = source.fields()
    return {
        **manifest,
        **fields,
        "inventory": {**manifest.get("inventory", {}), **fields["inventory"]},
        "artifact_hashes": {**manifest.get("artifact_hashes", {}), **fields["artifact_hashes"]},
        "exclusions": [*manifest.get("exclusions", []), *fields["exclusions"]],
    }


def checkpoint(validator: model.Validator, source: SourceCapture, manifest: dict) -> model.Verdict:
    """Every material is stored before the manifest; an interruption leaves the earlier checkpoint the current one."""
    for role, data in source.materials.items():
        validator.store.put(f"{manifest['checkpoint_id']}.{role}", data)
    return validator.commit(merged(manifest, source))


def _members(data: bytes) -> list[tuple[str, int, bytes]]:
    found = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for info in archive.getmembers():
            parts = PurePosixPath(info.name).parts
            if not info.isreg() or info.name.startswith("/") or {"..", ".git"}.intersection(parts):
                raise SourceError(ESCAPE)
            found.append((info.name, info.mode, archive.extractfile(info).read()))
    return found


def _extract(target: Path, members: list[tuple[str, int, bytes]]) -> None:
    for name, mode, data in members:
        try:
            path = contain(target, name)
        except LayoutError as error:
            raise SourceError(ESCAPE) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode & EXECUTABLE)


def restore(validator: model.Validator, checkpoint_id: str, target: Path) -> model.Verdict:
    """The target is a clean clone holding the base commit; nothing in it changes until every material is read."""
    verdict = validator.verify(checkpoint_id)
    if not verdict.authoritative:
        raise SourceError(f"checkpoint is not authoritative: {verdict.reason}")
    store = validator.store
    doc = json.loads(store.get_range(store.recorded(checkpoint_id)))
    materials = {role: store.get_range(ArtifactRef(**ref)) for role, ref in doc["inventory"].items()}
    members = _members(materials[UNTRACKED])
    if _git(target, "status", "--porcelain", "--untracked-files=all"):
        raise SourceError(DIRTY)
    _git(target, "cat-file", "-e", doc["base_commit"] + "^{commit}")
    if model.SOURCE_BUNDLE in materials:
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "source.bundle"
            bundle.write_bytes(materials[model.SOURCE_BUNDLE])
            _git(target, "fetch", "-q", str(bundle), "HEAD")
    _git(target, "checkout", "-q", "-B", doc["branch_name"], doc["head_commit"])
    if materials[STAGED]:
        _git(target, "apply", "--index", data=materials[STAGED])
    if materials[UNSTAGED]:
        _git(target, "apply", data=materials[UNSTAGED])
    _extract(target, members)
    return verdict
