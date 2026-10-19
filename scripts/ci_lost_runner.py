import argparse
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from scripts.ci_queue_retry import base_name, dependents

WORKFLOWS = Path(".github/workflows")
OPEN = ("in_progress", "queued")
RED = ("failure", "timed_out")
HOSTED_GROUP = "GitHub Actions"
GRACE_MINUTES = 10
CANCEL_POLLS = 18
POLL_SECONDS = 10


def _api(*args: str) -> str:
    return subprocess.run(["gh", "api", *args], stdout=subprocess.PIPE, text=True, check=True).stdout


def live_runners(org: str) -> set[str]:
    names = _api("--paginate", f"orgs/{org}/actions/runners?per_page=100", "--jq", ".runners[].name")
    return set(names.split())


def lost_jobs(jobs: list[dict], runners: set[str], now: datetime, grace: timedelta) -> list[dict]:
    return [
        job
        for job in jobs
        if job["status"] == "in_progress"
        and job.get("runner_name")
        and job["runner_name"] not in runners
        and job.get("runner_group_name") != HOSTED_GROUP
        and now - datetime.fromisoformat(job["started_at"]) >= grace
    ]


def job_ids(workflow: dict) -> dict[str, str]:
    return {spec.get("name", job_id): job_id for job_id, spec in workflow["jobs"].items()}


def stalled(jobs: list[dict], lost: list[dict], workflow: dict) -> tuple[bool, str]:
    if not lost:
        return False, "no job lost its runner"
    ids = job_ids(workflow)
    names = {base_name(job["name"]) for job in lost}
    blocked = set().union(*(dependents(workflow, ids[name]) for name in names if name in ids))
    for job in jobs:
        if job in lost or job["status"] not in OPEN:
            continue
        if job["status"] == "in_progress":
            return False, f"{job['name']} still runs on a live runner"
        if base_name(job["name"]) not in blocked:
            return False, f"{job['name']} still waits for a runner"
    return True, f"runner lost on {', '.join(job['name'] for job in lost)}"


def decide(run: dict, jobs: list[dict]) -> tuple[bool, str]:
    failed = [job["name"] for job in jobs if job.get("conclusion") in RED]
    if failed:
        return False, f"failed on its own: {', '.join(failed)}"
    if run["run_attempt"] != 1:
        return False, f"attempt {run['run_attempt']} lost its runner again"
    return True, "first attempt lost its runner"


def run_state(repo: str, run_id: int) -> dict:
    return json.loads(_api(f"repos/{repo}/actions/runs/{run_id}"))


def stop(repo: str, run_id: int) -> dict:
    for action in ("cancel", "force-cancel"):
        _api("-X", "POST", f"repos/{repo}/actions/runs/{run_id}/{action}")
        for _ in range(CANCEL_POLLS):
            time.sleep(POLL_SECONDS)
            run = run_state(repo, run_id)
            if run["status"] == "completed":
                return run
    return run


def jobs_of(repo: str, run_id: int) -> list[dict]:
    return json.loads(_api(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"))["jobs"]


def open_runs(repo: str, workflow: str) -> list[dict]:
    runs = []
    for status in OPEN:
        page = json.loads(_api(f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100&status={status}"))
        runs += page["workflow_runs"]
    return runs


def watch(repo: str, run: dict, runners: set[str], workflow: dict, grace: timedelta) -> dict:
    report = {"run": run["id"], "attempt": run["run_attempt"], "action": "none"}
    if run["status"] not in OPEN:
        return {**report, "reason": f"run completed with conclusion {run['conclusion']}"}
    jobs = jobs_of(repo, run["id"])
    lost = lost_jobs(jobs, runners, datetime.now(UTC), grace)
    stuck, reason = stalled(jobs, lost, workflow)
    if not stuck:
        return {**report, "reason": reason}
    if run_state(repo, run["id"])["run_attempt"] != run["run_attempt"]:
        return {**report, "reason": "the run was rerun meanwhile"}
    retry, why = decide(run, jobs)
    if stop(repo, run["id"])["status"] != "completed":
        return {**report, "action": "cancel", "reason": f"{reason}; the run did not end after force cancel"}
    if not retry:
        return {**report, "action": "cancel", "reason": f"{reason}; {why}"}
    _api("-X", "POST", f"repos/{repo}/actions/runs/{run['id']}/rerun-failed-jobs")
    return {**report, "action": "rerun", "reason": f"{reason}; {why}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workflow", default="test.yml")
    parser.add_argument("--run-id", type=int, action="append", default=[])
    parser.add_argument("--grace-minutes", type=float, default=GRACE_MINUTES)
    args = parser.parse_args(argv)
    workflow = yaml.safe_load((WORKFLOWS / args.workflow).read_text())
    runs = [run_state(args.repo, run_id) for run_id in args.run_id] or open_runs(args.repo, args.workflow)
    runners = live_runners(args.repo.split("/")[0])
    for run in runs:
        print(json.dumps(watch(args.repo, run, runners, workflow, timedelta(minutes=args.grace_minutes))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
