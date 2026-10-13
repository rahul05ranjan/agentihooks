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
OTHER_MODULE = "tests/context/test_swarm_prompt_hook.py::test_post_tool_use_hands_every_harness_call_to_the_outcome_record"
ORDERS = [
    ("bound master alone", [BOUND]),
    ("bound master alone, again", [BOUND]),
    ("bound session alone", [SESSION]),
    ("same module test, then bound master", [SAME_MODULE, BOUND]),
    ("same module test, then bound session", [SAME_MODULE, SESSION]),
    ("other module hook test, then bound master", [OTHER_MODULE, BOUND]),
    ("other module hook test, then bound master, again", [OTHER_MODULE, BOUND]),
]


def closed_return() -> int:
    lines = HOOK.read_text().splitlines()
    return next(n + 2 for n, text in enumerate(lines) if "ledger_gate.closed(state)" in text)


def measure(tmp_path: Path, index: int, nodes: list[str]) -> tuple[int, bool, str]:
    data = tmp_path / f"order-{index}.coverage"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_CORE"))}
    env["COVERAGE_FILE"] = str(data)
    command = [sys.executable, "-m", "coverage", "run", "--branch", f"--include={HOOK}", "-m", "pytest"]
    proc = subprocess.run(
        [*command, "-q", "-p", "no:cacheprovider", "-p", "no:randomly", *nodes],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    covered = CoverageData(basename=str(data))
    covered.read()
    path = next((p for p in covered.measured_files() if p.endswith("swarm_ledger/ledger_hook.py")), None)
    ran = path is not None and closed_return() in (covered.lines(path) or [])
    tail = (proc.stdout.strip().splitlines() or ["no output"])[-1]
    return proc.returncode, ran, tail


def test_t189_closed_return_coverage_depends_on_test_order(tmp_path):
    line = closed_return()
    rows = [f"ledger_hook.py line {line}: {HOOK.read_text().splitlines()[line - 1].strip()}"]
    for index, (label, nodes) in enumerate(ORDERS):
        code, ran, tail = measure(tmp_path, index, nodes)
        rows.append(f"{label}: pytest exit {code}, line {line} ran {ran} ({tail})")
    raise AssertionError("T189 PROBE\n" + "\n".join(rows))
