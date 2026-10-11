import json
from pathlib import Path

import pytest

from tests import sv2_hdr05_cases

EVIDENCE = Path(__file__).parents[3] / "evidence" / "SV2-HDR-05"


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_hdr05_cases.run_case(case), sv2_hdr05_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
