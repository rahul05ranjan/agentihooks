import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.swarm_v2.artifacts.base import ArtifactRef
from scripts.swarm_v2.checkpoints import git_capture, model
from tests import sv2_ckp02_cases as cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-CKP-02"
DESCRIBED = [
    "untracked files git ignores",
    "cache directories: .mypy_cache, .pytest_cache, .ruff_cache, .tox, .venv, __pycache__, node_modules",
    "compiled files: .pyc, .pyo",
    "credential files by name",
    "untracked files whose content matches a secret pattern",
    "links and other files that are not regular",
    "untracked files over 1048576 bytes",
    "untracked files past 16777216 bytes in total",
]


def _pushed(root: Path) -> Path:
    remote, work = root / "remote.git", root / "worker"
    cases.git(root, "init", "-q", "--bare", str(remote))
    cases.git(root, "init", "-q", str(work))
    cases.git(work, "checkout", "-q", "-b", cases.BRANCH)
    cases.write(work / "app.py", "print('base')\n")
    cases.git(work, "add", "app.py")
    cases.git(work, "commit", "-q", "-m", "base")
    cases.git(work, "remote", "add", "origin", str(remote))
    cases.git(work, "push", "-q", "origin", "HEAD:refs/heads/dev")
    cases.git(work, "fetch", "-q", "origin")
    return work


def _names(source: git_capture.SourceCapture) -> list[str]:
    return cases.members(source)


def _round_trip(root: Path, source: git_capture.SourceCapture) -> Path:
    validator = model.Validator(cases.store(root / "durable"))
    assert cases.commit(validator, source, "ckpt-src-0001").authoritative
    target = cases.clone(root, "restored")
    reader = model.Validator(cases.store(root / "durable", execution="e2", generation=3))
    assert git_capture.restore(reader, "ckpt-src-0001", target).authoritative
    return target


def test_policy_defaults_and_description():
    assert git_capture.Policy() == git_capture.Policy(1048576, 16777216)
    assert git_capture.Policy().describe() == DESCRIBED
    assert git_capture.Policy(5, 9).describe()[-2:] == [
        "untracked files over 5 bytes",
        "untracked files past 9 bytes in total",
    ]


def test_a_pushed_head_carries_no_bundle_and_restores_untracked_only(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "draft.md", "draft\n")
    source = git_capture.capture(work, "origin/dev")
    assert source.branch_state == git_capture.PUSHED
    assert source.base_commit == source.head_commit
    assert source.branch_name == cases.BRANCH
    assert sorted(source.materials) == [git_capture.STAGED, git_capture.UNSTAGED, git_capture.UNTRACKED]
    assert source.materials[git_capture.STAGED] == b""
    assert source.materials[git_capture.UNSTAGED] == b""
    target = _round_trip(tmp_path, source)
    assert (target / "draft.md").read_text() == "draft\n"
    assert cases.state(target)["tracked_status"] == []


def test_fields_and_metrics_describe_every_material(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "app.py", "print('changed')\n")
    source = git_capture.capture(work, "origin/dev", git_capture.Policy(10, 20))
    fields = source.fields()
    refs = {role: ArtifactRef.of(data) for role, data in source.materials.items()}
    assert fields == {
        "base_commit": source.base_commit,
        "head_commit": source.head_commit,
        "branch_name": cases.BRANCH,
        "branch_state": "pushed",
        "inventory": {role: {"sha256": ref.sha256, "size": ref.size} for role, ref in refs.items()},
        "artifact_hashes": {role: "sha256:" + ref.sha256 for role, ref in refs.items()},
        "exclusions": git_capture.Policy(10, 20).describe(),
    }
    assert source.metrics() == {"checkpoint_source_bytes": {role: ref.size for role, ref in refs.items()}}
    assert source.metrics()[git_capture.METRIC][git_capture.UNSTAGED] > 0


def test_merged_keeps_the_manifest_materials_and_exclusions(tmp_path):
    source = git_capture.capture(_pushed(tmp_path), "origin/dev")
    manifest = {
        "checkpoint_id": "c1",
        "base_commit": "a" * 40,
        "inventory": {"normalized_transcript": {"sha256": "b" * 64, "size": 3}},
        "artifact_hashes": {"normalized_transcript": "sha256:" + "b" * 64},
        "exclusions": ["environment variables"],
    }
    doc = git_capture.merged(manifest, source)
    fields = source.fields()
    assert doc["checkpoint_id"] == "c1"
    assert doc["base_commit"] == source.base_commit
    assert doc["inventory"] == {
        "normalized_transcript": manifest["inventory"]["normalized_transcript"],
        **fields["inventory"],
    }
    assert doc["artifact_hashes"] == {"normalized_transcript": "sha256:" + "b" * 64, **fields["artifact_hashes"]}
    assert doc["exclusions"] == ["environment variables", *fields["exclusions"]]
    assert git_capture.merged({}, source) == fields


def test_caches_credentials_links_and_secrets_are_counted_not_archived(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "__pycache__" / "app.cpython-312.pyc", "compiled\n")
    cases.write(work / "pkg" / "node_modules" / "x.js", "cached\n")
    cases.write(work / "loose.pyc", "compiled\n")
    cases.write(work / "loose.pyo", "compiled\n")
    cases.write(work / "deploy" / ".netrc", "machine fixture\n")
    cases.write(work / "token.txt", "slack " + "xox" + "b-" + "0123456789abcdef\n")
    cases.write(work / "keep.md", "keep\n")
    (work / "link.md").symlink_to(work / "keep.md")
    source = git_capture.capture(work, "origin/dev")
    assert _names(source) == ["keep.md"]
    assert source.exclusions[len(DESCRIBED) :] == (
        "excluded cache: 4",
        "excluded credential: 1",
        "excluded link: 1",
        "excluded secret: 1",
    )


def test_ignored_files_are_not_listed_and_binary_content_is_kept(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / ".gitignore", "*.log\n")
    cases.git(work, "add", ".gitignore")
    cases.git(work, "commit", "-q", "-m", "ignore logs")
    cases.write(work / "run.log", "ignored\n")
    (work / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    cases.write(work / "b.md", "b\n")
    cases.write(work / "a.md", "a\n")
    source = git_capture.capture(work, "origin/dev")
    assert _names(source) == ["a.md", "b.md", "blob.bin"]
    assert source.exclusions == tuple(DESCRIBED)
    target = _round_trip(tmp_path, source)
    assert (target / "blob.bin").read_bytes() == b"\xff\xfe\x00binary"
    assert not (target / "run.log").exists()


def test_file_and_total_limits_are_inclusive(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "a.md", "12345")
    cases.write(work / "b.md", "1234")
    cases.write(work / "c.md", "123456")
    assert _names(git_capture.capture(work, "origin/dev", git_capture.Policy(5, 9))) == ["a.md", "b.md"]
    excluded = git_capture.capture(work, "origin/dev", git_capture.Policy(5, 8))
    assert _names(excluded) == ["a.md"]
    assert excluded.exclusions[-2:] == ("excluded size: 1", "excluded total: 1")


def test_file_modes_survive_a_restore(tmp_path):
    work = _pushed(tmp_path)
    for name, mode in (("run.sh", 0o700), ("other.sh", 0o601), ("plain.md", 0o600)):
        cases.write(work / name, name)
        (work / name).chmod(mode)
    target = _round_trip(tmp_path, git_capture.capture(work, "origin/dev"))
    assert (target / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (target / "other.sh").stat().st_mode & 0o777 == 0o644
    assert (target / "plain.md").stat().st_mode & 0o777 == 0o644


def test_capture_from_a_subdirectory_uses_the_worktree_root(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "sub" / "draft.md", "draft\n")
    assert git_capture.capture(work / "sub", "origin/dev") == git_capture.capture(work, "origin/dev")


def test_patches_apply_under_hostile_diff_settings(tmp_path):
    work = _pushed(tmp_path)
    hostile = (("diff.noprefix", "true"), ("color.ui", "always"), ("diff.mnemonicPrefix", "true"))
    for key, value in hostile:
        cases.git(work, "config", key, value)
    cases.write(work / "app.py", "print('staged')\n")
    cases.git(work, "add", "app.py")
    cases.write(work / "app.py", "print('unstaged')\n")
    source = git_capture.capture(work, "origin/dev")
    for key, _ in hostile:
        cases.git(work, "config", "--unset", key)
    before = cases.state(work)
    target = _round_trip(tmp_path, source)
    assert cases.state(target) == before


def test_a_detached_head_is_refused(tmp_path):
    work = _pushed(tmp_path)
    cases.git(work, "checkout", "-q", "--detach")
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.capture(work, "origin/dev")
    assert str(raised.value) == "git symbolic-ref failed with exit 128"


def test_git_failures_name_the_command_and_exit_only(tmp_path):
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture._git(tmp_path, "status")
    assert str(raised.value) == "git status failed with exit 128"
    assert git_capture._git(tmp_path, "hash-object", "--stdin", data=b"x\n").strip() == (
        b"587be6b4c3f93f93c489c0111bba5596147a26cb"
    )


def test_restore_refuses_a_checkpoint_that_is_not_authoritative(tmp_path):
    reader = model.Validator(cases.store(tmp_path / "durable"))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(reader, "ckpt-src-0009", tmp_path)
    assert str(raised.value) == "checkpoint is not authoritative: manifest_absent"


def _committed(root: Path) -> tuple[model.Validator, Path]:
    work = cases.worktree(root)
    validator = model.Validator(cases.store(root / "durable"))
    assert cases.commit(validator, git_capture.capture(work, "origin/dev"), "ckpt-src-0001").authoritative
    return model.Validator(cases.store(root / "durable", execution="e2", generation=3)), work


def test_restore_refuses_a_dirty_target(tmp_path):
    reader, _ = _committed(tmp_path)
    target = cases.clone(tmp_path, "restored")
    cases.write(target / "mine.md", "mine\n")
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(reader, "ckpt-src-0001", target)
    assert str(raised.value) == git_capture.DIRTY
    assert cases.state(target)["untracked"] == ["mine.md"]


def test_restore_refuses_a_target_without_the_base(tmp_path):
    reader, _ = _committed(tmp_path)
    target = tmp_path / "unrelated"
    cases.git(tmp_path, "init", "-q", str(target))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(reader, "ckpt-src-0001", target)
    assert str(raised.value).startswith("git cat-file failed with exit ")


def _archive(info: tarfile.TarInfo, data: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        archive.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


def _member(name: str, kind: bytes = tarfile.REGTYPE, size: int = 1) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size if kind == tarfile.REGTYPE else 0
    if kind == tarfile.SYMTYPE:
        info.linkname = "/etc/passwd"
    return info


@pytest.mark.parametrize(
    "info",
    [
        _member("/abs.txt"),
        _member("../up.txt"),
        _member("a/../../up.txt"),
        _member(".git/hooks/post-checkout"),
        _member("nested/.git/config"),
        _member("link.txt", tarfile.SYMTYPE),
        _member("folder", tarfile.DIRTYPE),
    ],
)
def test_archive_members_outside_the_worktree_or_not_regular_are_refused(info):
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture._members(_archive(info, b"x" if info.isreg() else b""))
    assert str(raised.value) == git_capture.ESCAPE


def test_regular_members_are_read_with_their_mode():
    info = _member("dir/run.sh", size=2)
    info.mode = 0o755
    assert git_capture._members(_archive(info, b"ok")) == [("dir/run.sh", 0o755, b"ok")]


def test_extraction_through_a_tracked_link_is_refused(tmp_path):
    target, outside = tmp_path / "target", tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "out").symlink_to(outside)
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture._extract(target, [("out/planted.txt", 0o644, b"x")])
    assert str(raised.value) == git_capture.ESCAPE
    assert list(outside.iterdir()) == []
    git_capture._extract(target, [("in/kept.txt", 0o600, b"x")])
    assert (target / "in" / "kept.txt").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
