import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ci_mutation.runner import prepare_workspace


def main():
    root = Path.cwd()
    changed = subprocess.check_output(
        ["git", "-C", "../head", "diff", "--name-only", os.environ["RECEIPT_STATS_BASE"], "HEAD"], text=True
    ).splitlines()
    paths = [
        path
        for path in changed
        if path.startswith("scripts/swarm_ledger/") and path.endswith(".py") and (root / path).is_file()
    ]
    work = root / ".receipt-stats"
    work.mkdir()
    prepare_workspace(root, work, paths, ["tests/swarm_ledger/receipt_probe.py"])
    selection = work / "selection.json"
    selection.write_text(
        json.dumps(
            {
                path: {
                    "lines": list(range(1, len((root / path).read_text().splitlines()) + 1)),
                    "tests": ["tests/swarm_ledger/receipt_probe.py"],
                }
                for path in paths
            }
        )
    )
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "scripts.ci_mutation.selection",
            str(selection),
            "0",
            "1",
            "collect",
            str(work / "stats.json"),
            "receipt-stats",
            "0",
            "1",
        ],
        cwd=work,
    )


if __name__ == "__main__":
    raise SystemExit(main())
