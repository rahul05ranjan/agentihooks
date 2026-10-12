from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

TRUSTED = (
    "((github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor)'
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor)'
    ' || contains(fromJSON(\'["merge_group","push"]\'), github.event_name)'
    " && github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]')"
)


def pool(label):
    return "${{ " + TRUSTED + " && '" + label + "' || 'ubuntu-latest' }}"


MOVED = {
    ("test.yml", "split"): "anton-oss",
    ("test.yml", "unit"): "anton-oss",
    ("test.yml", "shard-check"): "anton-oss",
    ("test.yml", "test-count"): "anton-oss",
    ("test.yml", "mutation-stats"): "anton-oss-batch",
    ("test.yml", "mutation"): "anton-oss-batch",
    ("mutation-preflight.yml", "stats"): "anton-oss-batch",
    ("mutation-preflight.yml", "mutation-shard"): "anton-oss-batch",
}


def jobs(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


@pytest.mark.parametrize(("workflow", "job"), sorted(MOVED))
def test_heavy_jobs_run_on_the_isolated_anton_pool_only_for_trusted_actors(workflow, job):
    assert jobs(workflow)[job]["runs-on"] == pool(MOVED[(workflow, job)])


def test_no_workflow_reaches_an_anton_pool_except_through_the_trusted_expression():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            runs_on = str(job.get("runs-on", ""))
            if "anton" in runs_on:
                assert (path.name, name) in MOVED, f"{path.name}:{name}"


def test_pull_request_target_workflows_stay_on_hosted_runners():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        if "pull_request_target" in text:
            assert "anton" not in text, path.name
