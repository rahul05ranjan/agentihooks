import json
import time

from tests.swarm_ledger import test_api_v1 as cases
from tests.swarm_ledger.test_ledger_authority import live as live


def test_record_the_original_receipt_case_requests(live, monkeypatch):
    calls = []
    original = cases.send

    def send(*args, **kwargs):
        started = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            calls.append({"method": args[1], "path": args[2], "wall_seconds": time.monotonic() - started})

    monkeypatch.setattr(cases, "send", send)
    try:
        cases.test_latest_thousand_operation_receipts_remain_retry_safe(live)
    finally:
        print(json.dumps({"receipt_requests": calls}, indent=2))
