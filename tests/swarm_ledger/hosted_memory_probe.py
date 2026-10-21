import gc
import json
import tracemalloc

import pytest

from scripts.swarm_ledger.api import resources
from scripts.swarm_ledger.events.hub import Hub
from scripts.swarm_ledger.repository import rows
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.row_memory_replay import document


@pytest.mark.parametrize("operation", ["read", "export", "delta", "publish"])
def test_retained_allocations(tmp_path, operation):
    state = document()
    state["tasks"] = state["tasks"][:400]
    repository = SQLiteLedgerRepository(tmp_path / "ledgers.sqlite3")
    repository.import_document("memory", state)
    entry = repository.current("memory")
    hub = Hub()
    hub.open("memory", lambda: {"ledger": entry.state})
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    tracemalloc.start()
    try:
        baseline = tracemalloc.take_snapshot()
        for index in range(12):
            if operation == "read":
                resources.read(repository.current("memory").state, "tasks", {"limit": 250}, entry.views)
            elif operation == "export":
                repository.export_document("memory")
            elif operation == "delta":
                rows.diff({"tasks": []}, {"tasks": state["tasks"]})
            else:
                hub.publish_snapshot("memory", "ledger", {**state, "_meta": {"rev": index + 2}})
        retained = tracemalloc.get_traced_memory()[0]
        sites = tracemalloc.take_snapshot().compare_to(baseline, "lineno")[:8]
        gc.collect()
        collected = tracemalloc.get_traced_memory()[0]
        print(
            json.dumps(
                {
                    "operation": operation,
                    "retained": retained,
                    "collected": collected,
                    "sites": [str(site) for site in sites],
                }
            )
        )
    finally:
        tracemalloc.stop()
        if enabled:
            gc.enable()
