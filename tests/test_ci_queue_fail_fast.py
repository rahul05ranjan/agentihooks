import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_queue_fail_fast as fail_fast

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _job(name, conclusion=None):
    return {"name": name, "status": "completed" if conclusion else "queued", "conclusion": conclusion}


def _workflow_job():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["queue-fail-fast"]


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


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
        _job(fail_fast.SELF, "cancelled"),
    ]
    assert fail_fast.failed(jobs) is None


def test_the_run_settles_only_once_stage_budget_is_listed():
    assert not fail_fast.settled([_job("lint", "success"), _job("Gate — Required")])
    assert fail_fast.settled([_job("lint", "success"), _job(fail_fast.LAST)])


def test_watch_polls_until_a_graded_job_fails():
    reads = iter(
        [[_job("unit (3.12, 1)")], [_job("unit (3.12, 1)"), _job("lint", "success")], [_job("lint", "failure")]]
    )
    sleeps = []
    assert fail_fast.watch(lambda: next(reads), sleeps.append) == "lint"
    assert sleeps == [fail_fast.INTERVAL_S, fail_fast.INTERVAL_S]


def test_watch_names_a_failure_listed_beside_stage_budget():
    assert fail_fast.watch(lambda: [_job(fail_fast.LAST), _job("sonar", "failure")], pytest.fail) == "sonar"


def test_watch_stops_without_a_name_once_every_graded_job_settled():
    reads = iter([[_job("unit (3.11, 1)")], [_job("unit (3.11, 1)", "success"), _job(fail_fast.LAST)]])
    assert fail_fast.watch(lambda: next(reads), lambda _: None) is None


def test_watch_keeps_polling_through_a_failed_read(capsys):
    def read():
        if not calls:
            calls.append(1)
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502 \n")
        return [_job("lint", "failure")]

    calls = []
    sleeps = []
    assert fail_fast.watch(read, sleeps.append) == "lint"
    assert sleeps == [fail_fast.INTERVAL_S]
    assert capsys.readouterr().out == "::warning::Reading this run's jobs failed: HTTP 502\n"


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


def _main(tmp_path, monkeypatch, jobs):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    seen = []

    def read(repo, run, attempt):
        seen.append((repo, run, attempt))
        return jobs

    assert fail_fast.main(["--repo", "org/repo", "--run", "7", "--attempt", "1"], read) == 0
    assert seen == [("org/repo", "7", "1")]
    return output.read_text()


def test_main_names_the_failed_job_for_the_cancel_step(tmp_path, monkeypatch, capsys):
    assert _main(tmp_path, monkeypatch, [_job("lint", "failure")]) == "failed=lint\n"
    assert capsys.readouterr().out == "::error::lint failed, so Gate Required cannot pass; cancelling this queue run.\n"


def test_main_leaves_the_cancel_step_off_when_every_graded_job_settled(tmp_path, monkeypatch, capsys):
    assert _main(tmp_path, monkeypatch, [_job("lint", "success"), _job(fail_fast.LAST)]) == "failed=\n"
    assert capsys.readouterr().out == "Every graded job settled without a failure.\n"


def test_the_watcher_runs_only_on_merge_queue_runs_with_cancel_rights():
    job = _workflow_job()
    assert job["if"] == "github.event_name == 'merge_group'"
    assert job["permissions"] == {"contents": "read", "actions": "write"}
    assert "needs" not in job


def test_the_watcher_reads_on_the_app_token_and_cancels_on_its_own_token():
    job = _workflow_job()
    mint = _step(job, "Mint the tcc main ci App token")
    watch = _step(job, "Watch for the first failed graded job")
    cancel = _step(job, "Cancel this queue run so the queue drops the entry")
    assert mint["with"]["permission-actions"] == "read"
    assert watch["id"] == "watch"
    assert watch["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert "python3 -m scripts.ci_queue_fail_fast" in watch["run"]
    assert '--attempt "$GITHUB_RUN_ATTEMPT"' in watch["run"]
    assert cancel["if"] == "steps.watch.outputs.failed != ''"
    assert cancel["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert cancel["run"] == 'gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"'


def test_stage_budget_needs_every_graded_job_so_its_listing_means_all_settled():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    gate = next(job for job in jobs.values() if job.get("name") == "Gate — Required")
    assert set(gate["needs"]) - {fail_fast.LAST} <= set(jobs[fail_fast.LAST]["needs"])
