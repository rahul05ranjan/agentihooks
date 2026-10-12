import pytest

from scripts import ci_runner_trust

pytestmark = pytest.mark.unit

ACTORS = ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"]
GUARD = (
    "github.event_name != 'pull_request_target' && "
    "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && "
    "contains(fromJSON(needs.runner-trust.outputs.actors), github.actor) && "
    "contains(fromJSON(needs.runner-trust.outputs.actors), github.triggering_actor)"
)


def workflow(runner="anton", guard=GUARD):
    return {
        "on": {"pull_request": {}, "push": {}, "workflow_dispatch": {}},
        "jobs": {
            "runner-trust": {
                "uses": "the-cloud-clockwork/agentihooks/.github/workflows/runner-trust.yml@dev",
            },
            "build": {"runs-on": runner, "needs": ["runner-trust"], "if": guard, "steps": []},
        },
    }


def test_safe_hosted_and_guarded_self_hosted_jobs_pass():
    workflows = {"build.yml": workflow(), "hosted.yaml": {"jobs": {"test": {"runs-on": "ubuntu-latest"}}}}
    assert ci_runner_trust.check(workflows, ACTORS) == []


@pytest.mark.parametrize(
    "runner",
    [
        "anton",
        "anton-light",
        "anton-batch",
        "self-hosted",
        ["linux", "self-hosted"],
        {"group": "private", "labels": "linux"},
        "${{ inputs.runner }}",
    ],
)
@pytest.mark.parametrize(
    "removed",
    [
        "github.event_name != 'pull_request_target'",
        "(github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)",
        "contains(fromJSON(needs.runner-trust.outputs.actors), github.actor)",
        "contains(fromJSON(needs.runner-trust.outputs.actors), github.triggering_actor)",
    ],
)
def test_each_untrusted_admission_is_red(runner, removed):
    guard = GUARD.replace(removed + " && ", "").removesuffix(" && " + removed)
    problems = ci_runner_trust.check({"build.yml": workflow(runner, guard)}, ACTORS)
    assert any("build.yml/build" in problem and "runner trust" in problem for problem in problems)


@pytest.mark.parametrize(
    "guard",
    [
        GUARD + " || true",
        "false || " + GUARD,
        "'" + GUARD + "'",
        "contains('" + GUARD + "', github.actor)",
        "! (" + GUARD + ")",
    ],
)
def test_guard_text_cannot_be_used_to_bypass_admission(guard):
    assert ci_runner_trust.check({"build.yml": workflow(guard=guard)}, ACTORS)


@pytest.mark.parametrize("change", ["local", "head", "input", "missing", "no-need"])
def test_allowlist_must_come_from_protected_reusable_workflow(change):
    candidate = workflow()
    trust = candidate["jobs"]["runner-trust"]
    if change == "local":
        trust["uses"] = "./.github/workflows/runner-trust.yml"
    elif change == "head":
        trust["uses"] = trust["uses"].replace("@dev", "@head")
    elif change == "input":
        trust["with"] = {"actors": ACTORS + ["stranger"]}
    elif change == "missing":
        del candidate["jobs"]["runner-trust"]
    else:
        candidate["jobs"]["build"]["needs"] = []
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_target_workflow_cannot_ever_allocate_a_private_runner():
    candidate = workflow()
    candidate["on"] = {"pull_request_target": {}}
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_matrix_include_cannot_hide_a_private_runner():
    candidate = workflow("${{ matrix.runner }}", "true")
    candidate["jobs"]["build"]["strategy"] = {"matrix": {"runner": ["ubuntu-latest"], "include": [{"runner": "anton"}]}}
    assert ci_runner_trust.check({"build.yml": candidate}, ACTORS)


def test_base_allowlist_requires_exact_approved_identities():
    assert ci_runner_trust.validate_actors(ACTORS) == ACTORS
    with pytest.raises(ValueError, match="trusted actors"):
        ci_runner_trust.validate_actors(ACTORS + ["stranger"])


def test_actor_output_loads_the_protected_file(tmp_path, capsys):
    import json

    policy = tmp_path / ".github/workflows/trusted-actors.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps(ACTORS))
    assert ci_runner_trust.main(["--root", str(tmp_path), "--actors-output"]) == 0
    assert json.loads(capsys.readouterr().out.strip().removeprefix("actors=")) == ACTORS


def test_repository_workflows_are_graded():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert ci_runner_trust.main(["--root", str(root)]) == 0


def test_protected_loader_reads_only_dev_approvals():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/runner-trust.yml").read_text())
    job = workflow["jobs"]["actors"]
    assert job["runs-on"] == "ubuntu-latest"
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "dev"
    assert checkout["with"]["repository"] == "the-cloud-clockwork/agentihooks"
    assert any("--actors-output" in step.get("run", "") for step in job["steps"])
