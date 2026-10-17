import json
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.swarm import promotion_watch
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm.store import RedisStore, SwarmConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core, new_ledger  # noqa: E402
from scripts.swarm_ledger.api import schemas  # noqa: E402

pytestmark = pytest.mark.unit

WATCHED = "owner-proof"
DOCTOR = "owner-proof-doctor"
NOW = 1_000_000
RED = {
    "id": 900,
    "conclusion": "failure",
    "head_sha": "a" * 40,
    "html_url": "https://github.com/o/r/actions/runs/900",
}


class SchemaLedger(LedgerClient):
    def __init__(self, slugs):
        super().__init__()
        self.docs = {slug: new_ledger.build_doc({"title": "Promotion owner proof"}, "swarm") for slug in slugs}
        self.lock = threading.RLock()
        self.operations = []
        self.refuse = False

    def _call(self, slug, ops=None):
        with self.lock:
            doc = self.docs[slug]
            if ops is not None:
                checked = schemas.check_operations(
                    {"operation_id": ops[0]["id"], "ops": ops, "guards": {}},
                    ledger_core,
                    tuple(task["id"] for task in doc["tasks"]),
                )
                if self.refuse:
                    raise LedgerRefused("persistence refused")
                ctx = ledger_core.Context({"rev": 0, "stamps": {}, "members": {}}, NOW, slug)
                for op in checked:
                    assert ledger_core.apply_op(doc, op, ctx)
                assert not ctx.refused
                self.operations.extend((slug, op) for op in checked)
            return deepcopy(doc)


@pytest.fixture
def linked():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug in (WATCHED, DOCTOR):
        store.create(SwarmConfig(slug, "/repo", 1, 1))
    store.set_peer(WATCHED, DOCTOR)
    store.set_peer(DOCTOR, WATCHED)
    return store, SchemaLedger((WATCHED, DOCTOR))


def github(argv, **kwargs):
    body = [{"name": "coverage-ratchet", "conclusion": "failure", "steps": []}] if "/jobs" in argv[2] else [RED]
    return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(row) for row in body), "")


def observe(store, ledger, slug, now=NOW):
    return promotion_watch.refresh(slug, store.config(slug), store, ledger, ledger.state(slug), now, github)


def test_linked_swarms_file_one_fixer_in_the_watched_ledger_even_when_doctor_observes_first(linked):
    store, ledger = linked
    observe(store, ledger, DOCTOR)
    observe(store, ledger, WATCHED)
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert [(slug, task["id"]) for slug, doc in ledger.docs.items() for task in doc["tasks"]] == [
        (WATCHED, "promo-900-0")
    ]
    assert [(slug, op["kind"], op["lane"]) for slug, op in ledger.operations if op["op"] == "task_add"] == [
        (WATCHED, "ci", "ci")
    ]
    assert ledger.docs[DOCTOR].get("alerts", []) == []


@pytest.mark.parametrize("departure", ["stopped", "removed"])
def test_owner_departure_hands_over_without_recreating_an_existing_fixer_or_changing_its_proof(linked, departure):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    ledger.docs[WATCHED]["tasks"][0]["proof"] = {"run": "existing repair proof"}
    original = deepcopy(ledger.docs[WATCHED]["tasks"])
    if departure == "removed":
        store.remove(WATCHED)
    else:
        store.update(WATCHED, state="stopped")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[DOCTOR]["tasks"] == []
    assert ledger.docs[WATCHED]["tasks"] == original


@pytest.mark.parametrize("failure", ["ledger", "checkpoint"])
def test_failed_persistence_then_owner_departure_retries_without_duplicate_intake(linked, monkeypatch, failure):
    from scripts.swarm import promotion_owner

    store, ledger = linked
    record = promotion_owner.key("/repo")
    original_hset = store.redis.hset

    def hset(name, key=None, value=None, mapping=None, **kwargs):
        if name == record and key == "seen":
            raise RuntimeError("checkpoint refused")
        return original_hset(name, key, value, mapping=mapping, **kwargs)

    if failure == "ledger":
        ledger.refuse = True
        error = LedgerRefused
    else:
        monkeypatch.setattr(store.redis, "hset", hset)
        error = RuntimeError
    with pytest.raises(error):
        observe(store, ledger, WATCHED)
    assert store.redis.hget(record, "seen") is None
    assert len(ledger.docs[WATCHED]["tasks"]) == (failure == "checkpoint")
    ledger.refuse = False
    monkeypatch.setattr(store.redis, "hset", original_hset)
    store.update(WATCHED, state="stopped")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert sum(len(doc["tasks"]) for doc in ledger.docs.values()) == 1
    assert store.redis.hget(record, "seen") == "900"


def test_handover_on_a_new_failed_run_preserves_the_existing_active_repair(linked, monkeypatch):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    ledger.docs[WATCHED]["tasks"][0]["proof"] = {"run": "retained proof"}
    original = deepcopy(ledger.docs[WATCHED]["tasks"])
    store.update(WATCHED, state="stopped")
    monkeypatch.setitem(RED, "id", 901)
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[DOCTOR]["tasks"] == []
    assert ledger.docs[WATCHED]["tasks"] == original
