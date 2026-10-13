import json
import threading
import time
import uuid

from scripts.swarm_ledger.api.client import ResourceClient
from tests.ledger_load import gate

ISOLATED_READS = 10
MIXED_SECONDS = 60
COLLECTION_READS_PER_SECOND = 0.2


def collector(api, began, deadline, results):
    for n in range(10**6):
        start = began + n / COLLECTION_READS_PER_SECOND
        time.sleep(max(0.0, start - time.monotonic()))
        if start >= deadline:
            return
        try:
            sent = time.monotonic()
            api.collection(gate.SLUG, "tasks")
            results["collections"].append(time.monotonic() - sent)
        except Exception as exc:
            results["errors"].append(f"collector: {type(exc).__name__}: {exc}")


def test_t143_measure(tmp_path):
    doc = gate.full_size(gate.core.now_ms())
    token = gate.store(tmp_path, doc)
    port = gate.free_port()
    base = f"http://127.0.0.1:{port}"
    report = {"ledger_bytes": gate.size(doc), "tasks": len(doc["tasks"])}
    with (tmp_path / "server.log").open("w") as log:
        server = gate.start_server(tmp_path, port, log)
        try:
            name = "ci@ab0000-0099"
            api = ResourceClient(
                base,
                {"X-Ledger-Token": gate.authority.agent_token(token, gate.SLUG, name), "X-Ledger-Agent": name},
                gate.TIMEOUT_S,
            )
            api.mutate(gate.SLUG, [{"op": "join", "id": f"join-{uuid.uuid4().hex[:10]}", "by": name, "role": "member"}])
            isolated = {"errors": []}
            deadline = time.monotonic() + 120
            stream = threading.Thread(
                target=gate.watcher, args=(base, {"X-Ledger-Token": token}, deadline, isolated), daemon=True
            )
            stream.start()
            time.sleep(1)
            pages = len(api.collection(gate.SLUG, "tasks"))
            walls, cpu = [], gate.cpu_seconds(server.pid)
            for _ in range(ISOLATED_READS):
                sent = time.monotonic()
                api.collection(gate.SLUG, "tasks")
                walls.append(time.monotonic() - sent)
            report["collection_rows"] = pages
            report["isolated_cpu_per_collection_s"] = round((gate.cpu_seconds(server.pid) - cpu) / ISOLATED_READS, 4)
            report["isolated_collection_p50_s"] = round(sorted(walls)[len(walls) // 2], 4)
            report["isolated_collection_p95_s"] = round(gate.p95(walls), 4)
            results = {"collections": [], "errors": []}
            began = time.monotonic()
            cpu = gate.cpu_seconds(server.pid)
            reader = threading.Thread(target=collector, args=(api, began, began + MIXED_SECONDS, results))
            reader.start()
            mixed = gate.load(port, token, server.pid, MIXED_SECONDS)
            reader.join()
            elapsed = time.monotonic() - began
            writes = mixed["writes"]
            report["mixed"] = {
                "seconds": round(elapsed, 1),
                "server_cpu_s": round(gate.cpu_seconds(server.pid) - cpu, 3),
                "mean_cores": round((gate.cpu_seconds(server.pid) - cpu) / elapsed, 3),
                "busiest_cores": gate.busiest_window(mixed["cpu"]),
                "writes": len(writes),
                "write_p50_s": round(sorted(writes)[len(writes) // 2], 3),
                "write_p95_s": round(gate.p95(writes), 3),
                "collections": len(results["collections"]),
                "collection_p95_s": round(gate.p95(results["collections"]), 3),
                "errors": (isolated["errors"] + mixed["errors"] + results["errors"])[:5],
            }
        finally:
            gate.stop_server(server)
    raise AssertionError("T143 MEASURE " + json.dumps(report))
