import json
import os
import subprocess
import sys
from pathlib import Path

from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "swarm_ledger" / "ledger_hook.py"
DECISION = "tests/context/test_ledger_decision.py"
BOUND = f"{DECISION}::test_a_bound_master_appends_its_plan_to_its_own_ledger"
SESSION = f"{DECISION}::test_a_session_bound_to_a_ledger_gets_no_small_ledger"
SAME_MODULE = f"{DECISION}::test_a_task_list_of_four_directs_a_small_ledger"
PROMPT = f"{DECISION}::test_a_troubleshooting_prompt_directs_a_small_ledger"
OTHER_MODULE = "tests/context/test_swarm_prompt_hook.py::test_post_tool_use_hands_every_harness_call_to_the_outcome_record"

DRIVER = """
import json, os, sys
import pytest
plan = json.loads(sys.argv[1])
report = []
if plan["preimport"]:
    os.environ["LEDGER_DIR"] = plan["preimport"]
    from scripts.swarm_ledger import ledger_hook
    del os.environ["LEDGER_DIR"]
for nodes in plan["steps"]:
    code = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "no:randomly", *nodes])
    mod = sys.modules.get("scripts.swarm_ledger.ledger_hook")
    report.append({"nodes": [n.split("::")[-1] for n in nodes], "exit": int(code),
                   "hook_imported": mod is not None, "sessions": str(getattr(mod, "SESSIONS", None))})
print("DRIVER " + json.dumps(report))
"""

CASES = [
    ("bound master alone", None, [[BOUND]]),
    ("bound session alone", None, [[SESSION]]),
    ("hook imported first under another ledger folder, then bound master", "PRE", [[BOUND]]),
    ("hook imported first under another ledger folder, then bound session", "PRE", [[SESSION]]),
    ("todo test, then bound master", None, [[SAME_MODULE], [BOUND]]),
    ("prompt test, then bound master", None, [[PROMPT], [BOUND]]),
    ("other module post tool test, then bound master", None, [[OTHER_MODULE], [BOUND]]),
]


def closed_return() -> int:
    lines = HOOK.read_text().splitlines()
    return next(n + 2 for n, text in enumerate(lines) if "ledger_gate.closed(state)" in text)


def measure(tmp_path: Path, index: int, preimport, steps) -> str:
    data = tmp_path / f"case-{index}.coverage"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_CORE"))}
    env["COVERAGE_FILE"] = str(data)
    env["PYTHONPATH"] = str(ROOT)
    pre = str(tmp_path / f"other-{index}") if preimport else None
    if pre:
        (Path(pre) / ".sessions").mkdir(parents=True)
    plan = json.dumps({"preimport": pre, "steps": steps})
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--branch", f"--include={HOOK}", str(driver), plan],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    covered = CoverageData(basename=str(data))
    covered.read()
    path = next((p for p in covered.measured_files() if p.endswith("swarm_ledger/ledger_hook.py")), None)
    lines = set(covered.lines(path) or []) if path else set()
    report = next((line[7:] for line in proc.stdout.splitlines() if line.startswith("DRIVER ")), proc.stdout[-300:])
    return f"line {closed_return()} ran {closed_return() in lines}, session found {closed_return() - 12 in lines}; {report}"


def test_t189_closed_return_coverage_depends_on_first_import(tmp_path):
    line = closed_return()
    rows = [f"ledger_hook.py line {line}: {HOOK.read_text().splitlines()[line - 1].strip()}"]
    for index, (label, pre, steps) in enumerate(CASES):
        rows.append(f"{label}: {measure(tmp_path, index, pre, steps)}")
    raise AssertionError("T189 PROBE\n" + "\n".join(rows))
