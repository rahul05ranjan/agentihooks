import json
from pathlib import Path

import pytest

from scripts.swarm_v2.artifacts import local
from scripts.swarm_v2.artifacts.base import ABSENT, VERIFIED, ArtifactRef
from scripts.swarm_v2.checkpoints import model, publisher
from tests import sv2_ckp02_cases as cases

pytestmark = pytest.mark.unit


class LostAck:
    """Performs every mutating call, then reports a failure for the one numbered lose_at."""

    kind = "local"

    def __init__(self, root: Path, lose_at: int | None = None) -> None:
        self.inner = local.LocalBackend(root)
        self.lose_at = lose_at
        self.calls: list[tuple[str, str]] = []

    def _mutated(self, name: str, key: str) -> None:
        self.calls.append((name, key))
        if len(self.calls) == self.lose_at:
            raise ConnectionError(f"acknowledgement lost after {name} {key}")

    def write(self, key: str, data: bytes) -> None:
        self.inner.write(key, data)
        self._mutated("write", key)

    def move(self, source: str, target: str) -> None:
        self.inner.move(source, target)
        self._mutated("move", target)

    def remove(self, key: str) -> None:
        self.inner.remove(key)

    def size(self, key: str) -> int | None:
        return self.inner.size(key)

    def read(self, key: str, start: int, length: int) -> bytes:
        return self.inner.read(key, start, length)

    def keys(self, prefix: str) -> list[str]:
        return self.inner.keys(prefix)


def checkpoint(sequence: int, generation: int = 2, execution: str = "e1", payload: bytes | None = None):
    doc = cases.manifest_for(f"ckpt-g{generation}-s{sequence}")
    doc["checkpoint_sequence"] = sequence
    doc["authority"] = {**doc["authority"], "execution_id": execution, "task_generation": generation}
    doc["reconstruction"] = {**doc["reconstruction"], "execution_generation": generation}
    data = payload if payload is not None else json.dumps({"generation": generation, "sequence": sequence}).encode()
    return doc, {model.TRANSCRIPT: data}


def make(root: Path, execution: str = "e1", generation: int = 2, backend=None) -> publisher.Publisher:
    return publisher.Publisher(model.Validator(cases.store(root, execution, generation, backend)))


def files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def pointers(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if "/pointers/" in path.as_posix())


def test_publish_promotes_verified_checkpoints_in_sequence(tmp_path):
    pub = make(tmp_path)
    assert pub.latest() == ""
    assert pub.sequence() == 0
    first = pub.publish(*checkpoint(1))
    assert first.promoted is True
    assert first.reason == "promoted"
    assert first.verdict.authoritative
    assert pub.latest() == "ckpt-g2-s1"
    second = pub.publish(*checkpoint(2))
    assert (second.promoted, second.reason) == (True, "promoted")
    assert pub.latest() == "ckpt-g2-s2"
    assert pub.sequence() == 2
    assert pointers(tmp_path) == [
        "s1/t1/pointers/0000000002/0000000001.json",
        "s1/t1/pointers/0000000002/0000000002.json",
    ]
    assert pub.metrics() == {"checkpoint_publish_failures": {}}


def test_the_committed_manifest_lists_every_uploaded_material(tmp_path):
    pub = make(tmp_path)
    doc, materials = checkpoint(1)
    pub.publish(doc, materials)
    store = pub.validator.store
    committed = json.loads(store.get_range(store.recorded("ckpt-g2-s1")))
    ref = ArtifactRef.of(materials[model.TRANSCRIPT])
    assert committed["inventory"] == {model.TRANSCRIPT: {"sha256": ref.sha256, "size": ref.size}}
    assert committed["artifact_hashes"] == {model.TRANSCRIPT: "sha256:" + ref.sha256}
    assert store.recorded("ckpt-g2-s1.normalized_transcript") == ref
    pointer = json.loads((tmp_path / pointers(tmp_path)[0]).read_text())
    assert pointer == {
        "checkpoint_id": "ckpt-g2-s1",
        "checkpoint_sequence": 1,
        "execution_id": "e1",
        "generation": 2,
        "manifest": {"sha256": store.recorded("ckpt-g2-s1").sha256, "size": store.recorded("ckpt-g2-s1").size},
    }


def test_an_older_generation_is_refused_once_a_newer_generation_has_written(tmp_path):
    stale = make(tmp_path, "e1", 2)
    assert stale.publish(*checkpoint(1)).promoted
    newer = make(tmp_path, "e2", 3)
    assert newer.publish(*checkpoint(1, 3, "e2")).promoted
    before = files(tmp_path)
    refused = stale.publish(*checkpoint(2))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "superseded_generation", None)
    assert files(tmp_path) == before
    assert stale.metrics() == {"checkpoint_publish_failures": {"superseded_generation": 1}}
    assert stale.latest() == newer.latest() == "ckpt-g3-s1"


def test_a_generation_superseded_during_upload_never_writes_its_pointer(tmp_path):
    stale = make(tmp_path, "e1", 2)
    newer = make(tmp_path, "e2", 3)
    commit = stale.validator.commit

    def interleaved(doc):
        newer.publish(*checkpoint(1, 3, "e2"))
        return commit(doc)

    stale.validator.commit = interleaved
    refused = stale.publish(*checkpoint(1))
    assert (refused.promoted, refused.reason) == (False, "superseded_generation")
    assert refused.verdict.authoritative
    assert pointers(tmp_path) == ["s1/t1/pointers/0000000003/0000000001.json"]
    assert stale.latest() == "ckpt-g3-s1"


def test_latest_picks_the_newest_generation_over_a_higher_sequence(tmp_path):
    assert make(tmp_path, "e1", 2).publish(*checkpoint(9)).promoted
    newer = make(tmp_path, "e2", 3)
    assert newer.publish(*checkpoint(1, 3, "e2")).promoted
    assert make(tmp_path, "e1", 2).latest() == "ckpt-g3-s1"
    assert newer.sequence() == 1


@pytest.mark.parametrize("sequence", [1, 2])
def test_a_sequence_at_or_below_the_pointer_is_refused(tmp_path, sequence):
    pub = make(tmp_path)
    assert pub.publish(*checkpoint(2)).promoted
    doc, materials = checkpoint(sequence, payload=b"other")
    doc["checkpoint_id"] = doc["operation_id"] = "ckpt-other"
    before = files(tmp_path)
    refused = pub.publish(doc, materials)
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "stale_sequence", None)
    assert files(tmp_path) == before
    assert pub.latest() == "ckpt-g2-s2"


def test_a_replayed_publish_promotes_once(tmp_path):
    pub = make(tmp_path)
    assert pub.publish(*checkpoint(1)).promoted
    again = pub.publish(*checkpoint(1))
    assert (again.promoted, again.reason) == (True, "replayed")
    assert again.verdict.authoritative
    assert len(pointers(tmp_path)) == 1
    assert pub.metrics() == {"checkpoint_publish_failures": {}}


def test_a_document_outside_the_granted_scope_uploads_nothing(tmp_path):
    pub = make(tmp_path)
    refused = pub.publish(*checkpoint(1, execution="e9"))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "scope_mismatch", None)
    assert files(tmp_path) == {}
    assert pub.metrics() == {"checkpoint_publish_failures": {"scope_mismatch": 1}}


def test_another_execution_of_the_same_generation_is_refused(tmp_path):
    assert make(tmp_path, "e1", 2).publish(*checkpoint(1)).promoted
    other = make(tmp_path, "e9", 2)
    before = files(tmp_path)
    refused = other.publish(*checkpoint(2, execution="e9"))
    assert (refused.promoted, refused.reason) == (False, "scope_mismatch")
    assert files(tmp_path) == before


def test_an_uncommitted_manifest_is_never_promoted(tmp_path):
    pub = make(tmp_path)
    assert pub.publish(*checkpoint(1)).promoted
    doc, materials = checkpoint(2)
    doc["status"] = "uploading"
    refused = pub.publish(doc, materials)
    assert (refused.promoted, refused.reason) == (False, "not_committed")
    assert refused.verdict.reason == "not_committed"
    assert pub.latest() == "ckpt-g2-s1"
    assert len(pointers(tmp_path)) == 1
    assert pub.metrics() == {"checkpoint_publish_failures": {"not_committed": 1}}


def test_latest_skips_a_pointer_whose_object_no_longer_verifies(tmp_path):
    pub = make(tmp_path)
    pub.publish(*checkpoint(1))
    doc, materials = checkpoint(2)
    pub.publish(doc, materials)
    ref = ArtifactRef.of(materials[model.TRANSCRIPT])
    (tmp_path / "s1" / "t1" / "objects" / ref.sha256).write_bytes(b"torn")
    assert pub.latest() == "ckpt-g2-s1"


def test_latest_never_falls_back_to_an_older_generation(tmp_path):
    assert make(tmp_path, "e1", 2).publish(*checkpoint(1)).promoted
    newer = make(tmp_path, "e2", 3)
    doc, materials = checkpoint(1, 3, "e2")
    newer.publish(doc, materials)
    (tmp_path / "s1" / "t1" / "objects" / ArtifactRef.of(materials[model.TRANSCRIPT]).sha256).unlink()
    assert newer.latest() == ""


def test_latest_ignores_a_pointer_naming_other_manifest_content(tmp_path):
    pub = make(tmp_path)
    pub.publish(*checkpoint(1))
    pub.publish(*checkpoint(2))
    path = tmp_path / "s1/t1/pointers/0000000002/0000000002.json"
    pointer = json.loads(path.read_text())
    pointer["manifest"]["size"] += 1
    path.write_text(json.dumps(pointer))
    assert pub.latest() == "ckpt-g2-s1"


def _mutations(root: Path) -> list[tuple[str, str]]:
    backend = LostAck(root)
    pub = make(root, backend=backend)
    pub.publish(*checkpoint(1))
    backend.calls.clear()
    pub.publish(*checkpoint(2))
    return backend.calls


def test_a_lost_acknowledgement_at_every_boundary_keeps_a_verified_latest_and_replays_once(tmp_path):
    calls = _mutations(tmp_path / "count")
    assert [name for name, _ in calls].count("move") >= 4
    assert calls[-1][1] == "s1/t1/pointers/0000000002/0000000002.json"
    for lose_at in range(1, len(calls) + 1):
        root = tmp_path / f"lose-{lose_at}"
        backend = LostAck(root)
        pub = make(root, backend=backend)
        assert pub.publish(*checkpoint(1)).promoted
        backend.calls.clear()
        backend.lose_at = lose_at
        with pytest.raises(ConnectionError):
            pub.publish(*checkpoint(2))
        current = pub.latest()
        assert current == ("ckpt-g2-s2" if lose_at == len(calls) else "ckpt-g2-s1"), lose_at
        assert pub.validator.verify(current).authoritative
        backend.lose_at = None
        again = pub.publish(*checkpoint(2))
        assert again.promoted, lose_at
        assert again.reason == ("replayed" if lose_at == len(calls) else "promoted")
        assert pub.latest() == "ckpt-g2-s2"
        assert len(pointers(root)) == 2


def _crashed(root: Path, payload: bytes | None = None) -> publisher.Publisher:
    pub = make(root)
    assert pub.publish(*checkpoint(1, payload=b"first")).promoted

    def stopped(doc):
        raise ConnectionError("stopped before the manifest commit")

    commit = pub.validator.commit
    pub.validator.commit = stopped
    with pytest.raises(ConnectionError):
        pub.publish(*checkpoint(2, payload=payload))
    pub.validator.commit = commit
    return pub


def test_a_stop_before_manifest_commit_leaves_reclaimable_orphans_and_the_old_checkpoint(tmp_path):
    pub = _crashed(tmp_path, b"second")
    store = pub.validator.store
    assert store.recorded("ckpt-g2-s2") is None
    assert pub.latest() == "ckpt-g2-s1"
    assert pub.validator.verify("ckpt-g2-s1").authoritative
    assert pub.reclaim("ckpt-g2-s1") == []
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.normalized_transcript"]
    assert store.stat(ArtifactRef.of(b"second")) == ABSENT
    assert store.recorded("ckpt-g2-s2.normalized_transcript") is None
    assert store.stat(ArtifactRef.of(b"first")) == VERIFIED
    assert pub.validator.verify("ckpt-g2-s1").authoritative
    assert pub.reclaim("ckpt-g2-s2") == []


def test_reclaim_keeps_an_object_another_checkpoint_still_records(tmp_path):
    pub = _crashed(tmp_path, b"first")
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.normalized_transcript"]
    assert pub.validator.store.stat(ArtifactRef.of(b"first")) == VERIFIED
    assert pub.validator.verify("ckpt-g2-s1").authoritative


def test_reclaim_leaves_a_newer_generation_record(tmp_path):
    newer = make(tmp_path, "e2", 3)
    newer.validator.store.put("ckpt-g3-s1.normalized_transcript", b"newer")
    stale = make(tmp_path, "e1", 2)
    assert stale.reclaim("ckpt-g3-s1") == []
    assert newer.validator.store.stat(ArtifactRef.of(b"newer")) == VERIFIED
