import copy
import json
from datetime import date
from pathlib import Path

import pytest

from scripts import ci_wiring, gate_protected

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def combined(base):
    head = copy.deepcopy(base)
    jobs = head["test.yml"]["jobs"]
    durations = jobs.pop("durations")
    selection = jobs["select"]
    selection["outputs"] = durations["outputs"] | selection["outputs"]
    selection["steps"] = durations["steps"] + selection["steps"]
    selection["if"] = durations["if"]
    for job in jobs.values():
        if isinstance(job.get("needs"), list):
            job["needs"] = list(dict.fromkeys("select" if name == "durations" else name for name in job["needs"]))
    head["test.yml"]["jobs"] = json.loads(json.dumps(jobs).replace("needs.durations.outputs.", "needs.select.outputs."))
    step = head["test.yml"]["jobs"]["gate-required"]["steps"][0]
    step["run"] = (
        step["run"]
        .replace('["durations", "split",', '["select", "split",')
        .replace('["durations", "select",', '["select",')
    )
    return head


def grade(base, head):
    config = json.loads((ROOT / ci_wiring.CONFIG).read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    return gate_protected.grade(base, config, head, config, date(2026, 10, 16), policy)


@pytest.mark.parametrize("already_combined", [False, True])
def test_protected_grading_accepts_combined_duration_admission(already_combined):
    base = ci_wiring.load(ROOT)
    head = combined(base)
    if already_combined:
        base = copy.deepcopy(head)
    assert grade(base, head) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("env", {"PYTHONPATH": "/untrusted"}),
        ("defaults", {"run": {"working-directory": "/untrusted"}}),
        ("permissions", {"contents": "write"}),
        ("timeout-minutes", 60),
        ("outputs", {"key": "forged"}),
    ],
)
def test_combined_admission_keeps_its_protected_execution_context(field, value):
    base = ci_wiring.load(ROOT)
    head = combined(base)
    head["test.yml"]["jobs"]["select"][field] = value
    assert grade(base, head)


def test_combined_admission_keeps_provenance_before_head_execution():
    base = ci_wiring.load(ROOT)
    head = combined(base)
    head["test.yml"]["jobs"]["select"]["steps"].insert(0, {"run": "echo forged"})
    assert any("provenance" in problem for problem in grade(base, head))


def test_combined_admission_cannot_retire_another_required_gate():
    base = ci_wiring.load(ROOT)
    head = combined(base)
    head["test.yml"]["jobs"]["gate-required"]["needs"].remove("lint")
    assert any("base need lint" in problem for problem in grade(base, head))
