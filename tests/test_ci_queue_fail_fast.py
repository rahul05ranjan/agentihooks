import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import ci_budget
from scripts import ci_queue_fail_fast as fail_fast

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

_ARGS = ["--repo", "org/repo", "--run", "7", "--attempt", "1"]


def _job(name, conclusion=None, status=None):
    return {"name": name, "status": status or ("completed" if conclusion else "queued"), "conclusion": conclusion}


def _jobs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _gate_needs(jobs):
    return set(next(job for job in jobs.values() if job.get("name") == "Gate — Required")["needs"])


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(fail_fast.time, "sleep", slept.append)
    return slept


def test_the_watcher_skips_its_own_job_and_waits_on_stage_budget():
    assert fail_fast.SELF == "queue-fail-fast"
    assert fail_fast.LAST == "stage-budget"
    assert fail_fast.INTERVAL_S == 15


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_a_failed_timed_out_or_cancelled_job_is_named(conclusion):
    jobs = [_job("lint", "success"), _job("unit (3.12, 1)"), _job("swarm-image / smoke", conclusion)]
    assert fail_fast.failed(jobs) == "swarm-image / smoke"


def test_the_first_failed_job_listed_is_named():
    assert fail_fast.failed([_job("lint", "failure"), _job("sonar", "failure")]) == "lint"


def test_passing_skipped_and_running_jobs_and_the_watcher_itself_name_nothing():
    jobs = [
        _job("lint", "success"),
        _job("mutation", "skipped"),
        _job("unit (3.11, 2)"),
        _job("queue-fail-fast", "cancelled"),
    ]
    assert fail_fast.failed(jobs) is None


@pytest.mark.parametrize(
    "jobs, expected",
    [
        ([_job("lint", "success"), _job("Gate — Required", "success")], False),
        ([_job("stage-budget")], False),
        ([_job("stage-budget", status="in_progress")], False),
        ([_job("lint", "success"), _job("stage-budget", "success")], True),
    ],
)
def test_the_run_settles_only_once_stage_budget_completed(jobs, expected):
    assert fail_fast.settled(jobs) is expected


def test_watch_polls_until_a_graded_job_fails(sleeps):
    reads = iter(
        [[_job("unit (3.12, 1)")], [_job("unit (3.12, 1)"), _job("lint", "success")], [_job("lint", "failure")]]
    )
    assert fail_fast.watch(lambda: next(reads)) == "lint"
    assert sleeps == [15, 15]


def test_watch_names_a_failure_listed_beside_a_completed_stage_budget(sleeps):
    assert fail_fast.watch(lambda: [_job("stage-budget", "success"), _job("sonar", "failure")]) == "sonar"
    assert sleeps == []


def test_watch_stops_without_a_name_once_every_graded_job_settled(sleeps):
    reads = iter([[_job("stage-budget")], [_job("unit (3.11, 1)", "success"), _job("stage-budget", "success")]])
    assert fail_fast.watch(lambda: next(reads)) is None
    assert sleeps == [15]


def test_watch_keeps_polling_through_a_failed_read(capsys, sleeps):
    reads = iter([subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502 \n"), [_job("lint", "failure")]])

    def read():
        answer = next(reads)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert fail_fast.watch(read) == "lint"
    assert sleeps == [15]
    assert capsys.readouterr().out == "::warning::Reading this run's jobs failed: HTTP 502\n"


def test_watch_gives_up_after_ten_failed_reads_in_a_row(capsys, sleeps):
    error = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 401")
    answers = iter([error] * 9 + [[_job("lint")]] + [error] * 10)

    def read():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(subprocess.CalledProcessError):
        fail_fast.watch(read)
    assert fail_fast.MISSES == 10
    assert len(sleeps) == 19
    assert capsys.readouterr().out.count("::warning::") == 19


@pytest.mark.parametrize("error", [ValueError("bad json"), FileNotFoundError("gh")])
def test_watch_lets_any_other_read_error_end_the_watcher(error, sleeps):
    def read():
        raise error

    with pytest.raises(type(error)):
        fail_fast.watch(read)
    assert sleeps == []


def test_read_jobs_lists_every_job_of_this_attempt(monkeypatch):
    seen = {}

    def run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(_job("lint", "success")) + "\n\n")

    monkeypatch.setattr(fail_fast.subprocess, "run", run)
    assert fail_fast.read_jobs("org/repo", "7", "2") == [_job("lint", "success")]
    assert seen["args"] == [
        "gh",
        "api",
        "--paginate",
        "repos/org/repo/actions/runs/7/attempts/2/jobs?per_page=100",
        "--jq",
        ".jobs[] | @json",
    ]
    assert seen["kwargs"] == {"check": True, "capture_output": True, "text": True}


def _output(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.write_text("keep=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return output


def test_main_names_the_failed_job_for_the_cancel_step(tmp_path, monkeypatch, capsys):
    output = _output(tmp_path, monkeypatch)
    seen = []

    def read(repo, run, attempt):
        seen.append((repo, run, attempt))
        return [_job("lint", "failure")]

    assert fail_fast.main(_ARGS, read) == 0
    assert seen == [("org/repo", "7", "1")]
    assert output.read_text() == "keep=1\nfailed=lint\n"
    assert capsys.readouterr().out == "::error::lint failed, so Gate Required cannot pass; cancelling this queue run.\n"


def test_main_sleeps_between_polls_and_leaves_the_cancel_step_off_once_settled(tmp_path, monkeypatch, capsys, sleeps):
    output = _output(tmp_path, monkeypatch)
    reads = iter([[_job("lint")], [_job("lint", "success"), _job("stage-budget", "success")]])
    assert fail_fast.main(_ARGS, lambda *_: next(reads)) == 0
    assert sleeps == [15]
    assert output.read_text() == "keep=1\nfailed=\n"
    assert capsys.readouterr().out == "Every graded job settled without a failure.\n"


@pytest.mark.parametrize("flag", ["--repo", "--run", "--attempt"])
def test_main_requires_the_repo_run_and_attempt(flag, tmp_path, monkeypatch):
    _output(tmp_path, monkeypatch)
    index = _ARGS.index(flag)
    with pytest.raises(SystemExit) as raised:
        fail_fast.main(_ARGS[:index] + _ARGS[index + 2 :], pytest.fail)
    assert raised.value.code == 2


def test_the_module_reads_the_jobs_api_from_the_command_line(tmp_path, monkeypatch):
    output = _output(tmp_path, monkeypatch)
    calls = []

    def run(args, **_):
        calls.append(args[3])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(_job("lint", "failure")) + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["ci_queue_fail_fast", *_ARGS])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("scripts.ci_queue_fail_fast", run_name="__main__")
    assert raised.value.code == 0
    assert calls == ["repos/org/repo/actions/runs/7/attempts/1/jobs?per_page=100"]
    assert output.read_text() == "keep=1\nfailed=lint\n"


def test_the_watcher_runs_only_on_merge_queue_runs_with_cancel_rights():
    job = _jobs()["queue-fail-fast"]
    assert job["if"] == "github.event_name == 'merge_group'"
    assert job["permissions"] == {"contents": "read", "actions": "write"}
    assert job["timeout-minutes"] == 60
    assert job["runs-on"] == _jobs()["stage-budget"]["runs-on"]
    assert "needs" not in job


def test_the_watcher_reads_on_the_app_token_and_cancels_on_its_own_token():
    job = _jobs()["queue-fail-fast"]
    mint = _step(job, "Mint the tcc main ci App token")
    watch = _step(job, "Watch for the first failed graded job")
    cancel = _step(job, "Cancel this queue run so the queue drops the entry")
    assert job["steps"][0] == {
        "uses": "actions/checkout@v4",
        "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
    }
    assert mint["id"] == "app-token"
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "read",
        "permission-contents": "read",
    }
    assert watch["id"] == "watch"
    assert watch["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert watch["run"] == (
        'python3 -m scripts.ci_queue_fail_fast --repo "$GITHUB_REPOSITORY" --run "$GITHUB_RUN_ID"'
        ' --attempt "$GITHUB_RUN_ATTEMPT"'
    )
    assert cancel["if"] == "steps.watch.outputs.failed != ''"
    assert cancel["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert cancel["run"] == 'gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"'


def test_stage_budget_needs_every_graded_job_so_its_completion_means_all_settled():
    jobs = _jobs()
    assert _gate_needs(jobs) - {"stage-budget"} <= set(jobs["stage-budget"]["needs"])


def test_every_merge_queue_job_but_the_watcher_is_graded():
    jobs = _jobs()
    push_only = {
        job_id
        for job_id, job in jobs.items()
        if "github.event_name == 'push'" in str(job.get("if", "")) and "merge_group" not in str(job.get("if", ""))
    }
    queued = set(jobs) - push_only - {"admission", "gate-required", "queue-fail-fast"}
    assert push_only == {"refresh-durations", "flake-budget", "coverage-baseline"}
    assert queued <= _gate_needs(jobs)


def test_the_watcher_budget_is_the_whole_run():
    assert ci_budget.BUDGETS["queue-fail-fast"] == ci_budget.RUN_BUDGET_S


def test_the_stage_report_leaves_out_the_watcher():
    def job(name):
        return {"name": name, "created_at": "2026-10-13T08:00:00Z", "completed_at": "2026-10-13T08:40:00Z"}

    assert ci_budget.stages([job("queue-fail-fast"), job("lint")]) == {"lint": 2400.0}
