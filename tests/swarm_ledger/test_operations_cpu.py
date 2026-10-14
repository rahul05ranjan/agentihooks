import cProfile
import gzip
import hashlib
import io
import json
import pstats
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.api import mutations, resources, schemas
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.events import Hub
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG = "operations-replay"


def corpus():
    return json.loads(gzip.decompress(Path(__file__).with_name("operations_corpus.json.gz").read_bytes()))


def replay(tmp_path, monkeypatch, profiled=False):
    recorded = corpus()
    core = server.core
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(core, "now_ms", lambda: 1791939600000)
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "served_version", lambda: "replay")
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    monkeypatch.setattr("scripts.gates.talk.Budget._marks", lambda self: None)
    for name in ("relay_to_inbox", "doctor_phrase", "deliver_alerts"):
        monkeypatch.setattr(server, name, Mock())
    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=core)
    repository.import_document(SLUG, recorded["state"])
    monkeypatch.setattr(server, "repository", Publishing(repository, server.publish_ledger))
    server.HUB.open(SLUG, lambda: {"ledger": server.ledger_view(repository.get_document(SLUG))})
    outputs, seconds = [], []
    profiler = cProfile.Profile(timer=time.thread_time)
    for index, op in enumerate(recorded["ops"]):
        state = repository.get_document(SLUG)
        target = schemas.target(op)
        payload = {
            "operation_id": f"request-{index}",
            "ops": [op],
            "guards": {target: resources.resource_revision(state, target)},
        }
        if profiled:
            profiler.enable()
        start = time.thread_time()
        try:
            reply = mutations.apply(server, SLUG, "", payload)
        except APIError as exc:
            reply = {"status": exc.status, **exc.envelope()}
        seconds.append(time.thread_time() - start)
        if profiled:
            profiler.disable()
        persisted = repository.get_document(SLUG)
        outputs.append(
            {"reply": reply, "state": hashlib.sha256(json.dumps(persisted, sort_keys=True).encode()).hexdigest()}
        )
    stream = io.StringIO()
    if profiled:
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(25)
    return {
        "source": recorded["source"],
        "requests": len(seconds),
        "mean_cpu_seconds": sum(seconds) / len(seconds),
        "cpu_seconds": seconds,
        "outputs": outputs,
        "profile": stream.getvalue(),
    }


def test_operations_write_cpu_is_at_most_half_the_production_baseline(tmp_path, monkeypatch):
    with monkeypatch.context() as isolated:
        measured = replay(tmp_path / "measured", isolated)
    with monkeypatch.context() as isolated:
        profiled = replay(tmp_path / "profiled", isolated, profiled=True)
    assert measured["outputs"] == profiled["outputs"]
    report = dict(measured)
    report["profile"] = profiled["profile"]
    assert measured["mean_cpu_seconds"] <= 0.268092752, json.dumps(report, indent=2)
