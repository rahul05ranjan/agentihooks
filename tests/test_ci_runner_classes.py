import json
from pathlib import Path

import pytest

from scripts import ci_runner_classes, ci_runner_trust

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"actors": ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"], "queue_actor": "github-merge-queue[bot]"}


def routed(label, policy=POLICY):
    return ci_runner_trust.routed_runner(label, policy)


def workflow(runner="ubuntu-latest"):
    return {"on": {"pull_request": {}}, "jobs": {"build": {"runs-on": runner, "steps": []}}}


def classes(workflows, table):
    return ci_runner_classes.check(workflows, table, POLICY)


def trust(workflows):
    return ci_runner_trust.check(workflows, POLICY)


@pytest.mark.parametrize("cls", ["light", "standard", "batch"])
def test_a_classed_job_accepts_hosted_and_its_routed_runner(cls):
    label = ci_runner_classes.LABELS[cls]
    for runner in ("ubuntu-latest", routed(label)):
        assert classes({"b.yml": workflow(runner)}, {"b.yml/build": cls}) == []


def test_a_job_with_no_class_is_red():
    assert classes({"b.yml": workflow()}, {}) == ["b.yml/build: runner class missing"]


def test_reusable_calls_are_graded_through_their_callee():
    calls = {"jobs": {"call": {"uses": "./.github/workflows/x.yml"}}}
    assert classes({"b.yml": calls}, {}) == []


def test_a_stale_table_entry_is_red():
    problems = classes({"b.yml": workflow()}, {"b.yml/build": "light", "b.yml/gone": "batch"})
    assert problems == ["b.yml/gone: runner class names no job"]


def test_an_unknown_class_is_red():
    problems = classes({"b.yml": workflow()}, {"b.yml/build": "huge"})
    assert problems == ["b.yml/build: runner class huge is not one of hosted, dind, light, standard, batch"]


@pytest.mark.parametrize(
    "cls,label",
    [("light", "anton-oss"), ("light", "anton-oss-batch"), ("standard", "anton-oss-light"), ("batch", "anton-oss")],
)
def test_a_routed_runner_must_match_the_job_class(cls, label):
    problems = classes({"b.yml": workflow(routed(label))}, {"b.yml/build": cls})
    assert problems == [f"b.yml/build: runs-on must be ubuntu-latest or the routed {cls} runner"]


@pytest.mark.parametrize("runner", ["anton-oss-light", "anton", "ubuntu-24.04", "${{ inputs.runner }}"])
def test_any_other_runner_is_red(runner):
    problems = classes({"b.yml": workflow(runner)}, {"b.yml/build": "light"})
    assert problems == ["b.yml/build: runs-on must be ubuntu-latest or the routed light runner"]


@pytest.mark.parametrize("cls", ["hosted", "dind"])
def test_hosted_and_docker_jobs_cannot_route(cls):
    problems = classes({"b.yml": workflow(routed("anton-oss"))}, {"b.yml/build": cls})
    assert problems == [f"b.yml/build: runs-on must be ubuntu-latest for a {cls} job"]


def test_a_routed_runner_must_carry_the_protected_actor_list():
    other = {
        "actors": ["stranger", "agenticore-app[bot]", "tcc-main-ci[bot]"],
        "queue_actor": "github-merge-queue[bot]",
    }
    runner = routed("anton-oss-light", other)
    assert classes({"b.yml": workflow(runner)}, {"b.yml/build": "light"}) != []
    assert trust({"b.yml": workflow(runner)}) != []


@pytest.mark.parametrize("key", sorted(ci_runner_classes.PINNED_HOSTED))
def test_publish_pages_release_protected_gate_and_reuse_stay_hosted(key):
    file, job = key.split("/")
    workflows = {file: {"jobs": {job: {"runs-on": "ubuntu-latest"}}}}
    assert classes(workflows, {key: "hosted"}) == []
    assert classes(workflows, {key: "light"}) == [f"{key}: runner class must be hosted"]


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


def test_routed_runner_is_the_trusted_pool_expression_with_hosted_fallback():
    runner = routed("anton-oss-batch")
    assert runner.startswith("${{ (github.event_name != 'pull_request_target'")
    assert (
        """contains(fromJSON('["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]'), github.triggering_actor)"""
        in runner
    )
    assert (
        "github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]')" in runner
    )
    assert runner.endswith(" && 'anton-oss-batch' || 'ubuntu-latest' }}")


@pytest.mark.parametrize("label", ["anton-oss-light", "anton-oss", "anton-oss-batch"])
def test_runner_trust_accepts_the_routed_runner(label):
    assert trust({"b.yml": workflow(routed(label))}) == []


@pytest.mark.parametrize(
    "runner",
    [
        routed("anton"),
        routed("anton-batch"),
        routed("anton-oss").replace("'ubuntu-latest'", "'anton'"),
        routed("anton-oss").replace("github.triggering_actor)", "github.actor)"),
        routed("anton-oss").replace("github.event.pull_request.head.repo.full_name == github.repository", "true"),
        routed("anton-oss").replace(" && github.triggering_actor == 'github-merge-queue[bot]'", ""),
    ],
)
def test_runner_trust_rejects_a_routed_runner_that_can_leave_the_oss_sets(runner):
    assert trust({"b.yml": workflow(runner)}) != []


def test_runner_trust_needs_the_protected_policy_to_admit_a_routed_runner():
    assert ci_runner_trust.check({"b.yml": workflow(routed("anton-oss"))}) != []


def test_runner_trust_rejects_a_routed_runner_on_target_events():
    target = workflow(routed("anton-oss"))
    target["on"] = {"pull_request_target": {}}
    assert trust({"b.yml": target}) != []


def test_repository_jobs_all_carry_a_runner_class():
    assert ci_runner_classes.main(["--root", str(ROOT)]) == 0


def test_table_is_sorted_json_kept_under_github(tmp_path, capsys):
    table = json.loads((ROOT / ci_runner_classes.TABLE).read_text())
    assert list(table) == sorted(table)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/a.yml").write_text("jobs:\n  x:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ".github/workflows/b.yaml").write_text("jobs:\n  y:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ".github/workflows/trusted-actors.json").write_text(json.dumps(POLICY))
    (tmp_path / ci_runner_classes.TABLE).write_text(json.dumps({"a.yml/x": "light"}))
    assert ci_runner_classes.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "Runner classes: 2 workflows, 2 jobs, 1 problems" in out
    assert "::error::b.yaml/y: runner class missing" in out
