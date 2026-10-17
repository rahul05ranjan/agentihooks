import json
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.unit
NOW = 1792274400000


def api(records=(), jobs=(), fail=False):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if fail:
            raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
        url = urlsplit(args[2])
        query = parse_qs(url.query)
        page = int(query.get("page", [1])[0])
        if url.path.endswith("/jobs"):
            body = {"total_count": len(jobs), "jobs": list(jobs)[(page - 1) * 100 : page * 100]}
        elif url.path.endswith("/runs"):
            selected = list(records) if "created" in query else []
            body = {"total_count": len(selected), "workflow_runs": selected[(page - 1) * 100 : page * 100]}
        else:
            body = {"id": int(url.path.rsplit("/", 1)[1]), "status": "completed", "event": "push"}
        return SimpleNamespace(stdout=json.dumps(body))

    return run, calls


def test_repository_runs_and_all_job_pages_are_discovered_with_full_counts():
    from scripts.doctor import ci_speed_read

    records = [{"id": n, "status": "in_progress", "event": "push"} for n in range(101)]
    run, calls = api(records, [{"id": n} for n in range(101)])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run, max_calls=300)
    assert result["complete"] is True
    assert len(result["runs"]) == 101
    assert sum(len(r["jobs"]) for r in result["runs"]) == 10201
    assert result["api_calls"] == len(calls) == 209
    assert all(0 < kwargs["timeout"] <= 25 for _, kwargs in calls)


def test_prior_active_runs_are_read_after_their_completion_between_passes():
    from scripts.doctor import ci_speed_read

    run, calls = api(jobs=[{"id": 19}])
    result = ci_speed_read.read(NOW - 60000, NOW, pending=[42], run=run)
    assert result["complete"] is True
    assert result["runs"] == [{"id": 42, "status": "completed", "event": "push", "jobs": [{"id": 19}]}]
    assert any(args[2].endswith("/runs/42") for args, _ in calls)


def test_api_failure_is_explicit_instead_of_a_successful_zero():
    from scripts.doctor import ci_speed_read

    run, calls = api(fail=True)
    result = ci_speed_read.read(NOW - 60000, NOW, run=run)
    assert result["complete"] is False
    assert "API" in result["error"] and result["api_calls"] == len(calls) == 1


def test_request_budget_exhaustion_reports_an_incomplete_pass():
    from scripts.doctor import ci_speed_read

    run, calls = api([{"id": n} for n in range(101)])
    result = ci_speed_read.read(NOW - 60000, NOW, run=run, max_calls=1)
    assert result["complete"] is False
    assert "budget" in result["error"] and result["api_calls"] == len(calls) == 1


def test_active_runs_are_discovered_even_when_they_predate_the_window():
    from scripts.doctor import ci_speed_read

    calls = []

    def run(args, **kwargs):
        calls.append(args[2])
        if "status=queued" in args[2]:
            body = {"total_count": 1, "workflow_runs": [{"id": 88, "status": "queued", "event": "push"}]}
        elif "/jobs?" in args[2]:
            body = {"total_count": 1, "jobs": [{"id": 9}]}
        else:
            body = {"total_count": 0, "workflow_runs": []}
        return SimpleNamespace(stdout=json.dumps(body))

    result = ci_speed_read.read(NOW - 60000, NOW, pending=[], run=run)
    assert result["complete"] is True and result["runs"][0]["id"] == 88
    assert len(calls) == 7
