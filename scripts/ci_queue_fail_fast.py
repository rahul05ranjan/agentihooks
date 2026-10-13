import argparse
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

SELF = "queue-fail-fast"
# Needs every graded job, so once it completes nothing graded can still fail.
LAST = "stage-budget"
FATAL = ("failure", "timed_out", "cancelled")
INTERVAL_S = 15
MISSES = 10


def failed(jobs: list[dict]) -> str | None:
    return next((job["name"] for job in jobs if job["name"] != SELF and job.get("conclusion") in FATAL), None)


def settled(jobs: list[dict]) -> bool:
    return any(job["name"] == LAST and job["status"] == "completed" for job in jobs)


def read_jobs(repo: str, run: str, attempt: str) -> list[dict]:
    out = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100",
            "--jq",
            ".jobs[] | @json",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line]


def watch(read: Callable[[], list[dict]], sleep: Callable[[float], None] = time.sleep) -> str | None:
    misses = 0
    while True:
        try:
            jobs = read()
        except subprocess.CalledProcessError as error:
            misses += 1
            print(f"::warning::Reading this run's jobs failed: {error.stderr.strip()}")
            if misses == MISSES:
                raise
        else:
            misses = 0
            if name := failed(jobs):
                return name
            if settled(jobs):
                return None
        sleep(INTERVAL_S)


def main(argv: list[str] | None = None, read: Callable[[str, str, str], list[dict]] = read_jobs) -> int:
    parser = argparse.ArgumentParser(description="name the first failed job of a merge queue Tests run")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    name = watch(lambda: read(args.repo, args.run, args.attempt))
    if name:
        print(f"::error::{name} failed, so Gate Required cannot pass; cancelling this queue run.")
    else:
        print("Every graded job settled without a failure.")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
        out.write(f"failed={name or ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
