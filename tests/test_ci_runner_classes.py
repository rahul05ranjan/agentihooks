import json
from pathlib import Path

import pytest

from scripts import ci_runner_classes, ci_runner_trust

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
LOADER = {"uses": ci_runner_trust.LOADER}


def routed(label):
    return ci_runner_trust.routed_runner(label)


def workflow(runner="ubuntu-latest", needs=("runner-trust",)):
    return {
        "on": {"pull_request": {}},
        "jobs": {"runner-trust": dict(LOADER), "build": {"runs-on": runner, "needs": list(needs), "steps": []}},
    }


@pytest.mark.parametrize("cls", ["light", "standard", "batch"])
def test_a_classed_job_accepts_hosted_and_its_routed_runner(cls):
    label = ci_runner_classes.LABELS[cls]
    for runner in ("ubuntu-latest", routed(label)):
        assert ci_runner_classes.check({"b.yml": workflow(runner)}, {"b.yml/build": cls}) == []


def test_a_job_with_no_class_is_red():
    problems = ci_runner_classes.check({"b.yml": workflow()}, {})
    assert problems == ["b.yml/build: runner class missing"]


def test_reusable_calls_are_graded_through_their_callee():
    calls = {"jobs": {"call": {"uses": "./.github/workflows/x.yml"}, "loader": dict(LOADER)}}
    assert ci_runner_classes.check({"b.yml": calls}, {}) == []


def test_a_stale_table_entry_is_red():
    problems = ci_runner_classes.check({"b.yml": workflow()}, {"b.yml/build": "light", "b.yml/gone": "batch"})
    assert problems == ["b.yml/gone: runner class names no job"]


def test_an_unknown_class_is_red():
    problems = ci_runner_classes.check({"b.yml": workflow()}, {"b.yml/build": "huge"})
    assert problems == ["b.yml/build: runner class huge is not one of hosted, light, standard, batch"]


@pytest.mark.parametrize(
    "cls,label",
    [("light", "anton-oss"), ("light", "anton-oss-batch"), ("standard", "anton-oss-light"), ("batch", "anton-oss")],
)
def test_a_routed_runner_must_match_the_job_class(cls, label):
    problems = ci_runner_classes.check({"b.yml": workflow(routed(label))}, {"b.yml/build": cls})
    assert problems == [f"b.yml/build: runs-on must be ubuntu-latest or the routed {cls} runner"]


@pytest.mark.parametrize("runner", ["anton-oss-light", "anton", "ubuntu-24.04", "${{ inputs.runner }}"])
def test_any_other_runner_is_red(runner):
    problems = ci_runner_classes.check({"b.yml": workflow(runner)}, {"b.yml/build": "light"})
    assert problems == ["b.yml/build: runs-on must be ubuntu-latest or the routed light runner"]


def test_a_hosted_job_cannot_route():
    label = ci_runner_classes.LABELS["light"]
    problems = ci_runner_classes.check({"b.yml": workflow(routed(label))}, {"b.yml/build": "hosted"})
    assert problems == ["b.yml/build: runs-on must be ubuntu-latest for a hosted job"]


@pytest.mark.parametrize("key", sorted(ci_runner_classes.PINNED_HOSTED))
def test_publish_pages_release_protected_gate_and_reuse_stay_hosted(key):
    file, job = key.split("/")
    workflows = {file: {"jobs": {job: {"runs-on": "ubuntu-latest"}}}}
    assert ci_runner_classes.check(workflows, {key: "hosted"}) == []
    assert ci_runner_classes.check(workflows, {key: "light"}) == [f"{key}: runner class must be hosted"]


def test_pinned_hosted_jobs_cover_every_named_release_path():
    assert {
        "publish-pypi.yml/publish",
        "publish-pypi.yml/verify",
        "pages.yml/build",
        "pages.yml/deploy",
        "release.yml/release",
        "gate-protected.yml/grade",
        "test.yml/gate-required",
        "test.yml/reuse",
        "runner-trust.yml/actors",
    } <= ci_runner_classes.PINNED_HOSTED


def test_routed_runner_falls_back_to_hosted_for_untrusted_runs():
    runner = routed("anton-oss-batch")
    assert runner.startswith("${{ " + ci_runner_trust.GUARD + " && 'anton-oss-batch' || 'ubuntu-latest' }}")
    assert runner.endswith("'ubuntu-latest' }}")


@pytest.mark.parametrize("label", ["anton-oss-light", "anton-oss", "anton-oss-batch"])
def test_runner_trust_accepts_the_routed_runner(label):
    assert ci_runner_trust.check({"b.yml": workflow(routed(label))}) == []


@pytest.mark.parametrize(
    "runner",
    [
        routed("anton"),
        routed("anton-batch"),
        routed("anton-oss").replace("'ubuntu-latest'", "'anton'"),
        routed("anton-oss").replace("github.triggering_actor", "github.actor"),
    ],
)
def test_runner_trust_rejects_a_routed_runner_that_can_leave_the_oss_sets(runner):
    assert ci_runner_trust.check({"b.yml": workflow(runner)}) != []


def test_runner_trust_requires_the_loader_for_a_routed_runner():
    assert ci_runner_trust.check({"b.yml": workflow(routed("anton-oss"), needs=())}) != []
    no_loader = workflow(routed("anton-oss"))
    del no_loader["jobs"]["runner-trust"]
    assert ci_runner_trust.check({"b.yml": no_loader}) != []


def test_runner_trust_rejects_a_routed_runner_on_target_events():
    target = workflow(routed("anton-oss"))
    target["on"] = {"pull_request_target": {}}
    assert ci_runner_trust.check({"b.yml": target}) != []


def test_repository_jobs_all_carry_a_runner_class():
    assert ci_runner_classes.main(["--root", str(ROOT)]) == 0


def test_table_is_sorted_json_kept_under_github(tmp_path, capsys):
    table = json.loads((ROOT / ci_runner_classes.TABLE).read_text())
    assert list(table) == sorted(table)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/a.yml").write_text("jobs:\n  x:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ".github/workflows/b.yaml").write_text("jobs:\n  y:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ci_runner_classes.TABLE).write_text(json.dumps({"a.yml/x": "light"}))
    assert ci_runner_classes.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "Runner classes: 2 workflows, 2 jobs, 1 problems" in out
    assert "::error::b.yaml/y: runner class missing" in out
