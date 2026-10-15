from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_promotion_requires_all_push_checks_and_never_runs_for_a_red_grade():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    promote = workflow["jobs"]["promote"]
    assert promote["uses"] == "./.github/workflows/promote.yml"
    assert {"gate-required", "refresh-durations", "coverage-baseline", "flake-budget"} <= set(promote["needs"])
    assert "github.event_name == 'push'" in promote["if"]
    assert "github.ref == 'refs/heads/dev'" in promote["if"]
    assert "needs.gate-required.result == 'success'" in promote["if"]
    assert "!contains(needs.*.result, 'failure')" in promote["if"]
    assert "!contains(needs.*.result, 'cancelled')" in promote["if"]
