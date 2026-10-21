import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from scripts.swarm_ledger.api.client import ResourceClient
from tests.ledger_load import gate


def test_record_write_latency_components(monkeypatch, tmp_path):
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    request = ResourceClient.request
    load = gate.load
    requests = []

    def measured_request(self, slug, path, payload=None):
        began = time.monotonic()
        try:
            return request(self, slug, path, payload)
        finally:
            requests.append([path, time.monotonic() - began])

    def measured_load(port, token, pid, seconds):
        results = load(port, token, pid, seconds)
        query = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/metrics?view=writes&limit=1000",
            headers={"X-Ledger-Token": token},
        )
        with urllib.request.urlopen(query, timeout=10) as response:
            records = json.loads(response.read())
        print(json.dumps({"requests": requests, "writes": results["writes"], "server": records}))
        requests.clear()
        return results

    monkeypatch.setattr(ResourceClient, "request", measured_request)
    monkeypatch.setattr(gate, "load", measured_load)
    print("DISK")
    gate.run(tmp_path)
    print("MEMORY")
    with tempfile.TemporaryDirectory(dir="/dev/shm") as folder:
        gate.run(Path(folder))
    pytest.fail("Write latency diagnostic completed")
