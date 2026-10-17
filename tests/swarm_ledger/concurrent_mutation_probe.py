import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ci_mutation.runner import prepare_workspace

PATHS = [
    "scripts/swarm_ledger/api/schemas.py",
    "scripts/swarm_ledger/ledger.py",
    "scripts/swarm_ledger/api/client.py",
]
CASE = "tests/swarm_ledger/concurrent_tracing_case.py"
NAME = "test_sixteen_concurrent_adds_of_different_ids_all_land_without_a_conflict"
NODE = f"{CASE}::{NAME}"


def statistics(root, work, baseline=False, delay=False):
    work.mkdir()
    paths = [path for path in PATHS if (root / path).is_file()]
    prepare_workspace(root, work, paths, [CASE])
    (work / CASE).write_text(
        f"from tests.swarm_ledger.test_client_conflict_retry import authority_live, live, {NAME}\n"
    )
    if delay:
        (work / CASE).write_text(
            "import time\n"
            "from tests.swarm_ledger.test_api_v1 import authority_live, live, server\n"
            f"from tests.swarm_ledger.test_client_conflict_retry import {NAME} as concurrent_case\n\n"
            f"def {NAME}(live, monkeypatch):\n"
            "    post = server.Handler.do_POST\n"
            "    def delayed_post(handler):\n"
            "        time.sleep(11)\n"
            "        return post(handler)\n"
            "    monkeypatch.setattr(server.Handler, 'do_POST', delayed_post)\n"
            "    concurrent_case(live)\n"
        )
    if baseline:
        transport = work / "tests/swarm_ledger/test_ledger_authority.py"
        transport.write_text(transport.read_text().replace("MUTATION_TRACE_FACTOR = 7", "MUTATION_TRACE_FACTOR = 1"))
    selection = work / "selection.json"
    selection.write_text(
        json.dumps(
            {
                path: {"lines": list(range(1, len((root / path).read_text().splitlines()) + 1)), "tests": [CASE]}
                for path in paths
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci_mutation.selection",
            str(selection),
            "0",
            "1",
            "collect",
            str(work / "stats.json"),
            "concurrent-stats",
            "0",
            "1",
        ],
        cwd=work,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    (work / "stats.log").write_text(result.stdout)
    print(result.stdout, flush=True)
    if baseline:
        assert result.returncode != 0 and "TimeoutError: timed out" in result.stdout
        print(
            "Original ten second deadline control failed on concurrent adds with eleven second POST delay.", flush=True
        )
        return
    assert result.returncode == 0, f"concurrent mutation statistics failed with {result.returncode}"
    stats = json.loads((work / "stats.json").read_text())
    assert stats["results"] and all(result["status"] == 0 for result in stats["results"])
    covered = {name for result in stats["results"] for name, tests in result["tests"].items() if NODE in tests}
    assert covered
    print(json.dumps({"functions_covered_by_case": len(covered), "stats": stats}), flush=True)


if __name__ == "__main__":
    root = Path.cwd()
    statistics(root, root / ".receipt-mutation-proof", sys.argv[1] == "stats-baseline", sys.argv[1] != "stats")
