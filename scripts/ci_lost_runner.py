import argparse
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from scripts.ci_queue_retry import _api, base_name, dependents

WORKFLOWS = Path(".github/workflows")
OPEN = ("in_progress", "queued")
RED = ("failure", "timed_out")
HOSTED_GROUP = "GitHub Actions"
ONLINE = '.runners[] | select(.status == "online") | .name'
RUNS = ".workflow_runs[]"
GRACE_MINUTES = 10
CONFIRM_SECONDS = 120
CANCEL_POLLS = 18
POLL_SECONDS = 10


def live_runners(org: str) -> set[str]:
    names = _api("--paginate", f"orgs/{org}/actions/runners?per_page=100", "--jq", ONLINE)
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
    return {base_name(spec.get("name", job_id)): job_id for job_id, spec in workflow["jobs"].items()}


def stalled(jobs: list[dict], lost: list[dict], workflow: dict) -> tuple[bool, str]:
    if not lost:
        return False, "no job lost its runner"
    ids = job_ids(workflow)
    names = {base_name(job["name"]) for job in lost}
    waiting = set().union(*(dependents(workflow, ids[name]) for name in names if name in ids))
    blocked = {base_name(name) for name in waiting}
    for job in jobs:
        if job in lost or job["status"] == "completed":
            continue
        if job["status"] == "in_progress":
            return False, f"{job['name']} still runs on a live runner"
        if base_name(job["name"]) not in blocked:
            return False, f"{job['name']} still waits for a runner"
    return True, f"runner lost on {', '.join(job['name'] for job in lost)}"


def decide(run: dict, jobs: list[dict]) -> tuple[str, str]:
    failed = [job["name"] for job in jobs if job.get("conclusion") in RED]
    if failed:
        return "none", f"failed on its own: {', '.join(failed)}"
    if run["run_attempt"] != 1:
        return "cancel", f"attempt {run['run_attempt']} lost its runner again"
    return "rerun", "first attempt lost its runner"


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


def jobs_of(repo: str, run_id: int) -> dict:
    return json.loads(_api(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"))


def open_runs(repo: str, workflow: str) -> list[dict]:
    runs = []
    for status in OPEN:
        url = f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=100&status={status}"
        runs += [json.loads(line) for line in _api("--paginate", url, "--jq", RUNS).splitlines()]
    return runs


def assess(repo: str, run: dict, workflow: dict, grace: timedelta) -> tuple[bool, str, list[dict]]:
    page = jobs_of(repo, run["id"])
    jobs = page["jobs"]
    if page["total_count"] != len(jobs):
        return False, f"listed {len(jobs)} of {page['total_count']} jobs", jobs
    lost = lost_jobs(jobs, live_runners(repo.split("/")[0]), datetime.now(UTC), grace)
    stuck, reason = stalled(jobs, lost, workflow)
    return stuck, reason, jobs


def watch(repo: str, run: dict, workflow: dict, grace: timedelta) -> dict:
    report = {"run": run["id"], "attempt": run["run_attempt"], "action": "none"}
    if run["status"] not in OPEN:
        return {**report, "reason": f"run completed with conclusion {run['conclusion']}"}
    stuck, reason, jobs = assess(repo, run, workflow, grace)
    if stuck:
        time.sleep(CONFIRM_SECONDS)
        stuck, reason, jobs = assess(repo, run, workflow, grace)
    if not stuck:
        return {**report, "reason": reason}
    action, why = decide(run, jobs)
    if action == "none":
        return {**report, "reason": f"{reason}; {why}"}
    if run_state(repo, run["id"])["run_attempt"] != run["run_attempt"]:
        return {**report, "reason": "the run was rerun meanwhile"}
    if stop(repo, run["id"])["status"] != "completed":
        return {**report, "action": "cancel", "reason": f"{reason}; the run did not end after force cancel"}
    if action == "cancel":
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
    errors = 0
    for run in runs:
        try:
            report = watch(args.repo, run, workflow, timedelta(minutes=args.grace_minutes))
        except subprocess.CalledProcessError as error:
            errors += 1
            report = {"run": run["id"], "attempt": run["run_attempt"], "action": "error", "reason": str(error)}
        print(json.dumps(report))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
