import cProfile
import io
import json
import pstats
import time
import warnings
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.api import resources
from scripts.swarm_ledger.api.routes import ledger_read
from scripts.swarm_ledger.new_ledger import build_doc
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

pytestmark = pytest.mark.unit


def test_thousand_task_collection_bounds_page_encodes(tmp_path, monkeypatch):
    document = build_doc({"title": "Collection CPU"})
    document["_meta"] = {"rev": 1, "updated_at": 1, "applied": []}
    document["tasks"] = [
        {
            "id": f"t{index}",
            "title": f"Task {index}",
            "description": "Work details é 🚀 " * 200,
            "state": "open",
            "comments": [{"text": "Historical discussion " * 200}],
        }
        for index in range(1000)
    ]
    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    repository.import_document("cpu", document)
    host = SimpleNamespace(repository=repository)
    ledger_read(host, "cpu", "tasks", {"limit": 100})
    dumps = json.dumps
    encodes = []

    def counted(value, *args, **kwargs):
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            encodes.append(len(value["data"]))
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(resources.json, "dumps", counted)
    profiler = cProfile.Profile()
    query, rows, pages = {"limit": 100}, [], 0
    started = time.process_time()
    profiler.enable()
    while True:
        reply = ledger_read(host, "cpu", "tasks", query)
        rows.extend(reply["data"])
        pages += 1
        if reply["next_cursor"] is None:
            break
        query["cursor"] = reply["next_cursor"]
    profiler.disable()
    cpu = time.process_time() - started
    output = io.StringIO()
    pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(15)
    report = f"COLLECTION CPU seconds={cpu:.6f} pages={pages} page_encodes={len(encodes)}\n{output.getvalue()}"
    warnings.warn(report, stacklevel=1)
    assert [row["id"] for row in rows] == [f"t{index}" for index in range(1000)]
    assert pages >= 10
    assert len(encodes) <= 2 * pages, report
