"""Checkpoint publication: materials, then a verified manifest, then a write-once pointer under the task generation."""

import json
from collections import Counter
from dataclasses import asdict, dataclass

from scripts.swarm_v2.artifacts.base import ArtifactRef, ArtifactStore
from scripts.swarm_v2.checkpoints import model

METRIC = "checkpoint_publish_failures"
PROMOTED = "promoted"
REPLAYED = "replayed"
SUPERSEDED = "superseded_generation"
STALE = "stale_sequence"
SCOPE = "scope_mismatch"
WIDTH = 10


@dataclass(frozen=True)
class Publication:
    promoted: bool
    reason: str
    verdict: model.Verdict | None = None


@dataclass(frozen=True)
class Pointer:
    generation: int
    sequence: int
    document: dict


def _listed(doc: dict, materials: dict[str, bytes]) -> dict:
    refs = {role: asdict(ArtifactRef.of(data)) for role, data in materials.items()}
    hashes = {role: "sha256:" + ref["sha256"] for role, ref in refs.items()}
    return {
        **doc,
        "inventory": {**doc.get("inventory", {}), **refs},
        "artifact_hashes": {**doc.get("artifact_hashes", {}), **hashes},
    }


def _read(store: ArtifactStore, key: str) -> dict:
    return json.loads(store.backend.read(key, 0, store.backend.size(key)))


class Publisher:
    """Only a pointer makes a checkpoint current. It is written once its manifest verifies and is never replaced; a
    newer generation's pointer fences every older attempt, and the latest lookup reads the newest generation alone."""

    def __init__(self, validator: model.Validator) -> None:
        self.validator = validator
        self.failures: Counter[str] = Counter()

    @property
    def store(self) -> ArtifactStore:
        return self.validator.store

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(self.failures)}

    def refuse(self, reason: str, verdict: model.Verdict | None = None) -> Publication:
        self.failures[reason] += 1
        return Publication(False, reason, verdict)

    def publish(self, doc: dict, materials: dict[str, bytes]) -> Publication:
        scope, authority = self.store.scope, doc["authority"]
        claimed = (authority["task_id"], authority["execution_id"], authority["task_generation"])
        if claimed != (scope.task_id, scope.execution_id, scope.generation):
            return self.refuse(SCOPE)
        checkpoint_id, sequence = doc["checkpoint_id"], doc["checkpoint_sequence"]
        blocked = self._blocked(checkpoint_id, sequence)
        if blocked == REPLAYED:
            return Publication(True, REPLAYED, self.validator.verify(checkpoint_id))
        if blocked:
            return self.refuse(blocked)
        for role, data in materials.items():
            self.store.put(f"{checkpoint_id}.{role}", data)
        verdict = self.validator.commit(_listed(doc, materials))
        if not verdict.authoritative:
            return self.refuse(verdict.reason, verdict)
        if self._newest() > scope.generation:
            return self.refuse(SUPERSEDED, verdict)
        self._point(checkpoint_id, sequence)
        return Publication(True, PROMOTED, verdict)

    def latest(self) -> str:
        found = self._pointers()
        newest = [pointer for pointer in found if pointer.generation == self._newest(found)]
        for pointer in sorted(newest, key=lambda pointer: pointer.sequence, reverse=True):
            if self._current(pointer.document):
                return pointer.document["checkpoint_id"]
        return ""

    def sequence(self) -> int:
        own = [pointer.sequence for pointer in self._pointers() if pointer.generation == self.store.scope.generation]
        return max(own, default=0)

    def reclaim(self, checkpoint_id: str) -> list[str]:
        """Removes the materials of a checkpoint whose manifest never committed; an object another record names stays."""
        if self.store.recorded(checkpoint_id) is not None:
            return []
        scope, backend = self.store.scope, self.store.backend
        prefix = scope.key("artifacts") + "/"
        records = {
            key.removeprefix(prefix).removesuffix(".json"): _read(self.store, key) for key in backend.keys(prefix)
        }
        owned = sorted(
            name
            for name, record in records.items()
            if name.startswith(checkpoint_id + ".") and record["generation"] <= scope.generation
        )
        kept = {record["sha256"] for name, record in records.items() if name not in owned}
        for name in owned:
            backend.remove(f"{prefix}{name}.json")
            if records[name]["sha256"] not in kept:
                backend.remove(scope.key("objects", records[name]["sha256"]))
        return owned

    def _blocked(self, checkpoint_id: str, sequence: int) -> str | None:
        scope = self.store.scope
        found = self._pointers()
        if self._newest(found) > scope.generation:
            return SUPERSEDED
        own = [pointer for pointer in found if pointer.generation == scope.generation]
        if any(pointer.document["execution_id"] != scope.execution_id for pointer in own):
            return SCOPE
        if any(pointer.document["checkpoint_id"] == checkpoint_id and pointer.sequence == sequence for pointer in own):
            return REPLAYED
        if any(pointer.sequence >= sequence for pointer in own):
            return STALE
        return None

    def _newest(self, found: list[Pointer] | None = None) -> int:
        listed = self._pointers() if found is None else found
        return max((pointer.generation for pointer in listed), default=0)

    def _pointers(self) -> list[Pointer]:
        found = []
        for key in self.store.backend.keys(self.store.scope.key("pointers") + "/"):
            generation, sequence = key.removesuffix(".json").split("/")[-2:]
            found.append(Pointer(int(generation), int(sequence), _read(self.store, key)))
        return found

    def _current(self, document: dict) -> bool:
        checkpoint_id = document["checkpoint_id"]
        if self.store.recorded(checkpoint_id) != ArtifactRef(**document["manifest"]):
            return False
        return self.validator.verify(checkpoint_id).authoritative

    def _point(self, checkpoint_id: str, sequence: int) -> None:
        scope = self.store.scope
        document = {
            "checkpoint_id": checkpoint_id,
            "checkpoint_sequence": sequence,
            "execution_id": scope.execution_id,
            "generation": scope.generation,
            "manifest": asdict(self.store.recorded(checkpoint_id)),
        }
        key = scope.key("pointers", f"{scope.generation:0{WIDTH}d}", f"{sequence:0{WIDTH}d}.json")
        self.store.backend.write(key, json.dumps(document, sort_keys=True).encode())
