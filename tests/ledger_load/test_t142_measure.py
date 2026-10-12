import json
import threading
import time
import uuid

from scripts.swarm_ledger.api.client import ResourceClient
from tests.ledger_load import gate

ISOLATED_EXPORTS = 20
MIXED_SECONDS = 60
EXPORTS_PER_SECOND = 0.4


def exporter(api, deadline, results):
    for n in range(10**6):
        began = results["began"] + n / EXPORTS_PER_SECOND
        time.sleep(max(0.0, began - time.monotonic()))
        if began >= deadline:
            return
        try:
            sent = time.monotonic()
            api.request(gate.SLUG, "export", {})
            results["exports"].append(time.monotonic() - sent)
        except Exception as exc:
            results["errors"].append(f"exporter: {type(exc).__name__}: {exc}")


def test_t142_measure(tmp_path):
    doc = gate.full_size(gate.core.now_ms())
    token = gate.store(tmp_path, doc)
    port = gate.free_port()
    base = f"http://127.0.0.1:{port}"
    report = {"ledger_bytes": gate.size(doc), "tasks": len(doc["tasks"])}
    with (tmp_path / "server.log").open("w") as log:
        server = gate.start_server(tmp_path, port, log)
        try:
            apis = []
            for n in range(gate.CLIENTS + 1):
                name = f"ci@ab0000-{n:04d}"
                credentials = {
                    "X-Ledger-Token": gate.authority.agent_token(token, gate.SLUG, name),
                    "X-Ledger-Agent": name,
                }
                api = ResourceClient(base, credentials, gate.TIMEOUT_S)
                api.mutate(
                    gate.SLUG, [{"op": "join", "id": f"join-{uuid.uuid4().hex[:10]}", "by": name, "role": "member"}]
                )
                apis.append((api, name, f"tasks/t{n * (gate.TASKS // gate.CLIENTS)}"))
            export_api = apis.pop()[0]
            export_api.request(gate.SLUG, "export", {})
            walls, cpu = [], gate.cpu_seconds(server.pid)
            for _ in range(ISOLATED_EXPORTS):
                sent = time.monotonic()
                export_api.request(gate.SLUG, "export", {})
                walls.append(time.monotonic() - sent)
            report["isolated_cpu_per_export_s"] = round((gate.cpu_seconds(server.pid) - cpu) / ISOLATED_EXPORTS, 4)
            report["isolated_export_p50_s"] = round(sorted(walls)[len(walls) // 2], 4)
            report["isolated_export_p95_s"] = round(gate.p95(walls), 4)
            results = {"writes": [], "reads": [], "errors": [], "cpu": [], "exports": []}
            began = time.monotonic()
            deadline, results["began"] = began + MIXED_SECONDS, began
            cpu = gate.cpu_seconds(server.pid)
            threads = [
                threading.Thread(
                    target=gate.client,
                    args=(*entry, began + n * gate.write_every() / gate.CLIENTS, deadline, results),
                )
                for n, entry in enumerate(apis)
            ]
            threads.append(threading.Thread(target=exporter, args=(export_api, deadline, results)))
            threads.append(threading.Thread(target=gate.sample_cpu, args=(server.pid, deadline, results)))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            elapsed = time.monotonic() - began
            report["mixed"] = {
                "seconds": round(elapsed, 1),
                "server_cpu_s": round(gate.cpu_seconds(server.pid) - cpu, 3),
                "mean_cores": round((gate.cpu_seconds(server.pid) - cpu) / elapsed, 3),
                "busiest_cores": gate.busiest_window(results["cpu"]),
                "writes": len(results["writes"]),
                "write_p50_s": round(sorted(results["writes"])[len(results["writes"]) // 2], 3),
                "write_p95_s": round(gate.p95(results["writes"]), 3),
                "exports": len(results["exports"]),
                "export_p95_s": round(gate.p95(results["exports"]), 3),
                "errors": results["errors"][:5],
            }
        finally:
            gate.stop_server(server)
    raise AssertionError("T142 MEASURE " + json.dumps(report))
