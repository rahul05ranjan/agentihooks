import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from scripts import ci_lost_runner

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
GRACE = timedelta(minutes=10)
NOW = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
RUNNERS = {"anton-oss-light-live"}
RUN = {"id": 7, "run_attempt": 1, "status": "queued", "conclusion": None}
RUNS = "repos/o/r/actions/runs/7"
JOBS = "repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100"
RUNNER_LIST = (
    "--paginate",
    "orgs/o/actions/runners?per_page=100",
    "--jq",
    '.runners[] | select(.status == "online") | .name',
)


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes):
    return stamp(datetime.now(UTC) - timedelta(minutes=minutes))


def job(name, status="completed", conclusion="success", runner="anton-oss-light-live", started=None, group="anton-oss"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "runner_name": runner,
        "runner_group_name": group,
        "started_at": started or stamp(NOW - timedelta(hours=1)),
    }


def lost(name="queue-fail-fast", started=None):
    return job(name, "in_progress", None, "anton-oss-light-gone", started or stamp(NOW - timedelta(hours=1)))


def gate():
    return job("Gate — Required", "queued", None, None)


def stalled_run():
    return [job("lint"), job("unit (3.12, 1)"), job("docs", conclusion="skipped"), lost(started=ago(60)), gate()]


class Api:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "-X":
            return ""
        value = self.pages[args if args[0] == "--paginate" else args[0]]
        return value.pop(0) if isinstance(value, list) else value

    def posts(self):
        return [call[2] for call in self.calls if call[0] == "-X"]


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(ci_lost_runner.time, "sleep", slept.append)
    return slept


def test_api_runs_gh_and_fails_on_a_refused_call(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="out")

    monkeypatch.setattr(ci_lost_runner.subprocess, "run", run)
    assert ci_lost_runner._api("a", "b") == "out"
    assert seen == {"argv": ["gh", "api", "a", "b"], "stdout": subprocess.PIPE, "text": True, "check": True}


def test_live_runners_reads_every_page_of_organization_runners(monkeypatch):
    api = Api({RUNNER_LIST: "anton-a\nanton-b\n"})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.live_runners("o") == {"anton-a", "anton-b"}
    assert api.calls == [RUNNER_LIST]


def test_a_job_in_progress_on_an_absent_runner_past_the_grace_is_lost():
    jobs = [lost(started=stamp(NOW - GRACE)), job("lint")]
    assert ci_lost_runner.lost_jobs(jobs, RUNNERS, NOW, GRACE) == [jobs[0]]


@pytest.mark.parametrize(
    "candidate",
    [
        lost(started=stamp(NOW - GRACE + timedelta(seconds=1))),
        job("queue-fail-fast", "in_progress", None),
        job("queue-fail-fast", "in_progress", None, None),
        job("durations", "in_progress", None, "GitHub Actions 1000223300", group="GitHub Actions"),
        job("queue-fail-fast", "in_progress", None, ""),
        job("queue-fail-fast", "queued", None, "anton-oss-light-gone"),
        job("queue-fail-fast", "completed", "success", "anton-oss-light-gone"),
    ],
)
def test_a_young_live_unassigned_or_finished_job_is_not_lost(candidate):
    assert ci_lost_runner.lost_jobs([candidate], RUNNERS, NOW, GRACE) == []


def test_job_ids_map_display_names_to_workflow_keys():
    ids = ci_lost_runner.job_ids(WORKFLOW)
    assert ids["Gate — Required"] == "gate-required"
    assert ids["queue-fail-fast"] == "queue-fail-fast"


def test_a_run_waiting_only_on_its_lost_job_is_stalled():
    jobs = stalled_run()
    stuck, reason = ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW)
    assert stuck
    assert reason == "runner lost on queue-fail-fast"


def test_a_run_without_a_lost_job_is_not_stalled():
    assert ci_lost_runner.stalled(stalled_run(), [], WORKFLOW) == (False, "no job lost its runner")


def test_a_sibling_on_a_live_runner_keeps_the_run_going():
    jobs = [*stalled_run(), job("unit (3.12, 2)", "in_progress", None)]
    assert ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW) == (
        False,
        "unit (3.12, 2) still runs on a live runner",
    )


def test_a_queued_job_that_does_not_need_the_lost_job_keeps_the_run_going():
    jobs = [*stalled_run(), job("unit (3.12, 3)", "queued", None, None)]
    assert ci_lost_runner.stalled(jobs, [jobs[3]], WORKFLOW) == (False, "unit (3.12, 3) still waits for a runner")


def test_a_lost_job_missing_from_the_workflow_blocks_no_queued_job():
    jobs = [lost("renamed-job"), gate()]
    assert ci_lost_runner.stalled(jobs, [jobs[0]], WORKFLOW) == (False, "Gate — Required still waits for a runner")


def test_every_lost_job_is_named():
    jobs = [lost("unit (3.12, 1)"), lost("unit (3.11, 2)"), gate()]
    assert ci_lost_runner.stalled(jobs, jobs[:2], WORKFLOW) == (True, "runner lost on unit (3.12, 1), unit (3.11, 2)")


def test_a_first_attempt_that_only_lost_its_runner_is_rerun():
    assert ci_lost_runner.decide(RUN, stalled_run()) == (True, "first attempt lost its runner")


@pytest.mark.parametrize("conclusion", ["failure", "timed_out"])
def test_a_run_that_failed_on_its_own_is_never_rerun(conclusion):
    jobs = [*stalled_run(), job("unit (3.12, 4)", conclusion=conclusion), job("lint (2)", conclusion="failure")]
    assert ci_lost_runner.decide(RUN, jobs) == (False, "failed on its own: unit (3.12, 4), lint (2)")


def test_a_cancelled_sibling_is_not_a_failure_of_its_own():
    assert ci_lost_runner.decide(RUN, [*stalled_run(), job("mutation", conclusion="cancelled")])[0]


def test_a_second_loss_is_not_rerun():
    assert ci_lost_runner.decide({**RUN, "run_attempt": 2}, stalled_run()) == (
        False,
        "attempt 2 lost its runner again",
    )


def test_stop_returns_once_a_cancel_ends_the_run(monkeypatch, no_sleep):
    api = Api({RUNS: [json.dumps(RUN), json.dumps({**RUN, "status": "completed"})]})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7)["status"] == "completed"
    assert api.posts() == [f"{RUNS}/cancel"]
    assert no_sleep == [ci_lost_runner.POLL_SECONDS] * 2


def test_stop_force_cancels_when_a_cancel_does_not_end_the_run(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 2)
    states = [json.dumps(RUN)] * 3 + [json.dumps({**RUN, "status": "completed"})]
    api = Api({RUNS: states})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7)["status"] == "completed"
    assert api.posts() == [f"{RUNS}/cancel", f"{RUNS}/force-cancel"]
    assert len(no_sleep) == 4


def test_stop_returns_the_last_state_when_nothing_ends_the_run(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    api = Api({RUNS: [json.dumps(RUN), json.dumps({**RUN, "status": "in_progress"})]})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.stop("o/r", 7)["status"] == "in_progress"
    assert api.posts() == [f"{RUNS}/cancel", f"{RUNS}/force-cancel"]


def test_open_runs_lists_running_and_queued_runs_of_the_workflow(monkeypatch):
    base = "repos/o/r/actions/workflows/test.yml/runs?per_page=100&status="
    api = Api(
        {
            f"{base}in_progress": json.dumps({"workflow_runs": [{"id": 1}]}),
            f"{base}queued": json.dumps({"workflow_runs": [{"id": 2}, {"id": 3}]}),
        }
    )
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    assert ci_lost_runner.open_runs("o/r", "test.yml") == [{"id": 1}, {"id": 2}, {"id": 3}]


def watched(monkeypatch, run, jobs, states):
    api = Api({JOBS: json.dumps({"jobs": jobs}), RUNS: [json.dumps(state) for state in states]})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    return api, ci_lost_runner.watch("o/r", run, RUNNERS, WORKFLOW, GRACE)


def test_a_stalled_first_attempt_is_cancelled_then_rerun_once(monkeypatch, no_sleep):
    api, report = watched(monkeypatch, RUN, stalled_run(), [RUN, {**RUN, "status": "completed"}])
    assert report == {
        "run": 7,
        "attempt": 1,
        "action": "rerun",
        "reason": "runner lost on queue-fail-fast; first attempt lost its runner",
    }
    assert api.posts() == [f"{RUNS}/cancel", f"{RUNS}/rerun-failed-jobs"]


def test_a_second_loss_is_cancelled_and_files_nothing(monkeypatch, no_sleep):
    second = {**RUN, "run_attempt": 2}
    api, report = watched(monkeypatch, second, stalled_run(), [second, {**second, "status": "completed"}])
    assert report["action"] == "cancel"
    assert report["reason"] == "runner lost on queue-fail-fast; attempt 2 lost its runner again"
    assert api.posts() == [f"{RUNS}/cancel"]


def test_a_stalled_run_with_its_own_failure_is_cancelled_but_not_rerun(monkeypatch, no_sleep):
    jobs = [*stalled_run(), job("unit (3.12, 4)", conclusion="failure")]
    api, report = watched(monkeypatch, RUN, jobs, [RUN, {**RUN, "status": "completed"}])
    assert report["action"] == "cancel"
    assert report["reason"].endswith("; failed on its own: unit (3.12, 4)")
    assert api.posts() == [f"{RUNS}/cancel"]


def test_a_run_that_will_not_end_is_reported_and_not_rerun(monkeypatch, no_sleep):
    monkeypatch.setattr(ci_lost_runner, "CANCEL_POLLS", 1)
    api, report = watched(monkeypatch, RUN, stalled_run(), [RUN, RUN, RUN])
    assert report == {
        "run": 7,
        "attempt": 1,
        "action": "cancel",
        "reason": "runner lost on queue-fail-fast; the run did not end after force cancel",
    }
    assert api.posts() == [f"{RUNS}/cancel", f"{RUNS}/force-cancel"]


def test_a_run_rerun_meanwhile_is_left_alone(monkeypatch, no_sleep):
    api, report = watched(monkeypatch, RUN, stalled_run(), [{**RUN, "run_attempt": 2}])
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": "the run was rerun meanwhile"}
    assert api.posts() == []


def test_a_run_still_working_is_left_alone(monkeypatch, no_sleep):
    jobs = [*stalled_run(), job("unit (3.12, 2)", "in_progress", None)]
    api, report = watched(monkeypatch, RUN, jobs, [])
    assert report["action"] == "none"
    assert report["reason"] == "unit (3.12, 2) still runs on a live runner"
    assert api.posts() == []


def test_a_lost_job_inside_its_grace_is_left_alone(monkeypatch, no_sleep):
    jobs = [job("lint"), lost(started=ago(9)), gate()]
    api, report = watched(monkeypatch, RUN, jobs, [])
    assert report["reason"] == "no job lost its runner"
    assert api.posts() == []


@pytest.mark.parametrize("conclusion", ["failure", "success", "cancelled"])
def test_a_completed_run_is_left_alone(monkeypatch, conclusion):
    done = {**RUN, "status": "completed", "conclusion": conclusion}
    api = Api({})
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    report = ci_lost_runner.watch("o/r", done, RUNNERS, WORKFLOW, GRACE)
    assert report == {"run": 7, "attempt": 1, "action": "none", "reason": f"run completed with conclusion {conclusion}"}
    assert api.calls == []


def test_main_watches_the_named_runs_against_the_organization_runners(monkeypatch, capsys, no_sleep):
    done = {**RUN, "id": 8, "status": "completed", "conclusion": "failure"}
    pages = {
        RUNS: [json.dumps(RUN), json.dumps(RUN), json.dumps({**RUN, "status": "completed"})],
        "repos/o/r/actions/runs/8": json.dumps(done),
        JOBS: json.dumps({"jobs": [job("lint"), lost(started=ago(11)), gate()]}),
        RUNNER_LIST: "anton-oss-light-live\n",
    }
    api = Api(pages)
    monkeypatch.setattr(ci_lost_runner, "_api", api)
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--run-id", "7", "--run-id", "8"]) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(report["run"], report["action"]) for report in reports] == [(7, "rerun"), (8, "none")]
    assert reports[1]["reason"] == "run completed with conclusion failure"
    assert RUNNER_LIST in api.calls


def test_main_scans_open_runs_of_the_named_workflow_with_its_grace(monkeypatch, capsys):
    base = "repos/o/r/actions/workflows/test.yml/runs?per_page=100&status="
    pages = {
        f"{base}in_progress": json.dumps({"workflow_runs": []}),
        f"{base}queued": json.dumps({"workflow_runs": [RUN]}),
        JOBS: json.dumps({"jobs": [job("lint"), lost(started=ago(11)), gate()]}),
        RUNNER_LIST: "",
    }
    monkeypatch.setattr(ci_lost_runner, "_api", Api(pages))
    monkeypatch.chdir(ROOT)
    assert ci_lost_runner.main(["--repo", "o/r", "--workflow", "test.yml", "--grace-minutes", "11.5"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "no job lost its runner"


def test_the_watchdog_workflow_runs_the_script_every_ten_minutes_with_the_app_token():
    workflow = yaml.safe_load((ROOT / ".github/workflows/lost-runner.yml").read_text())
    assert workflow[True]["schedule"] == [{"cron": "*/10 * * * *"}]
    steps = workflow["jobs"]["watch"]["steps"]
    mint = next(step for step in steps if step.get("id") == "app-token")
    assert mint["with"]["permission-actions"] == "write"
    assert mint["with"]["permission-organization-self-hosted-runners"] == "read"
    assert steps[-1]["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert steps[-1]["run"] == 'python -m scripts.ci_lost_runner --repo "$GITHUB_REPOSITORY"'
