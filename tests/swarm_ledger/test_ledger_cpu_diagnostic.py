import copy
import cProfile
import gc
import io
import pstats
import time
import tracemalloc
import weakref
from types import SimpleNamespace

import pytest


def document():
    tasks = []
    for index in range(1599):
        tasks.append(
            {
                "id": f"t{index}",
                "title": "a" * 60,
                "description": "b" * 1000,
                "state": "open",
                "done": False,
                "phase": "p1",
                "lane": "eng",
                "claimed_by": "",
                "issue_url": "",
                "pr_url": "",
                "depends_on": [],
                "territory": [],
                "kind": "code",
                "contract": {"must": "c" * 100, "check": "d" * 100, "judge": "reader"},
                "comments": [{"id": f"c{index}-{n}", "by": "reader", "at": n, "text": "e" * 100} for n in range(12)],
            }
        )
    doc = {
        "size": "swarm",
        "title": "diagnostic",
        "overview": "f" * 6000,
        "tasks": tasks,
        "phases": [{"id": f"p{n}", "title": "phase", "comments": []} for n in range(86)],
        "followups": [{"id": f"f{n}", "text": "g" * 220, "done": False} for n in range(1439)],
        "notifications": [{"id": f"n{n}", "text": "h" * 220} for n in range(1075)],
        "chat": [{"id": f"m{n}", "text": "i" * 220} for n in range(500)],
        "notes": [],
        "questions": [],
        "priorities": [],
        "artifacts": [],
        "alerts": [],
    }
    seed = copy.deepcopy(doc)
    seed["tasks"] = seed["tasks"][:500]
    seed["followups"] = seed["followups"][:500]
    seed["notifications"] = seed["notifications"][:500]
    seed["chat"] = seed["chat"][:100]
    doc["_meta"] = {
        "rev": 2,
        "created_at": 1,
        "updated_at": 2,
        "members": {},
        "seeds": {"1": copy.deepcopy(seed), "2": copy.deepcopy(seed)},
        "events": [{"rev": n, "kind": "comment", "text": "j" * 200} for n in range(2000)],
    }
    return doc


def profile(label, call, count=4):
    output = io.StringIO()
    profiler = cProfile.Profile()
    started, cpu = time.monotonic(), time.process_time()
    profiler.enable()
    for _ in range(count):
        call()
    profiler.disable()
    output.write(f"\n{label}: wall={time.monotonic() - started:.3f} cpu={time.process_time() - cpu:.3f}\n")
    pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(15)
    return output.getvalue()


def test_cpu_and_heap_replay(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger_server as server
    from scripts.swarm_ledger.api.routes import ledger_read
    from scripts.swarm_ledger.events.hub import Hub
    from scripts.swarm_ledger.events.publishing import Publishing
    from scripts.swarm_ledger.repository.rows import assemble, flatten
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repo = SQLiteLedgerRepository(tmp_path / "diagnostic.sqlite3")
    doc = document()
    shape = len(flatten(doc))
    repo.import_document("diagnostic", doc)
    del doc
    gc.collect()
    host = SimpleNamespace(repository=repo)
    report = [f"SHAPE rows={shape} document_bytes={len(repo._cache[repo._key('diagnostic')].text)}"]
    repo._cache.clear()
    report.append(profile("cold reconstruction", lambda: repo.get_document("diagnostic"), 1))
    report.append(profile("collection without stream", lambda: ledger_read(host, "diagnostic", "tasks", {})))
    report.append(profile("single task without stream", lambda: ledger_read(host, "diagnostic", "tasks/t1", {})))
    report.append(profile("export with seeds", lambda: repo.export_document("diagnostic")))
    report.append(profile("write without stream", lambda: repo.apply_ops("diagnostic", ops=[])))
    hub = Hub()
    monkeypatch.setattr(server, "HUB", hub)
    host.repository = Publishing(repo, server.publish_ledger)
    hub.open("diagnostic", lambda: {"ledger": server.ledger_view(repo.get_document("diagnostic"))})
    report.append(profile("collection with stream", lambda: ledger_read(host, "diagnostic", "tasks", {})))
    report.append(profile("write with stream", lambda: host.repository.apply_ops("diagnostic", ops=[])))
    gc.collect()
    tracemalloc.start()
    for _ in range(8):
        repo.export_document("diagnostic")
    before = tracemalloc.get_traced_memory()
    report.append(f"HEAP before_collect current_peak={before}")
    report.extend(str(row) for row in tracemalloc.take_snapshot().statistics("lineno")[:12])
    collected = gc.collect()
    after = tracemalloc.get_traced_memory()
    report.append(f"HEAP after_collect current_peak={after} collected={collected}")
    tracemalloc.stop()

    class Rows(dict):
        pass

    rows = Rows(flatten({"tasks": [{"id": "one", "description": "k" * 10000}]}))
    reference = weakref.ref(rows)
    assemble(rows)
    del rows
    report.append(f"ASSEMBLE rows retained after return={reference() is not None}")
    gc.collect()
    report.append(f"ASSEMBLE rows retained after collect={reference() is not None}")
    report.append(f"LISTEN backlog={server.ThreadingHTTPServer.request_queue_size}")
    pytest.fail("\n".join(report), pytrace=False)
