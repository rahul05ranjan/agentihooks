from datetime import datetime, timezone

import pytest

from scripts.doctor import cli as doctor
from scripts.swarm import cli as swarm_cli
from tests.doctor.test_doctor_cli import WATCHED, env  # noqa: F401

pytestmark = pytest.mark.unit
NOW = 1792274400000
HOUR_MS = 3600000


def stamp(seconds):
    return datetime.fromtimestamp((NOW + seconds * 1000) / 1000, timezone.utc).isoformat()


def job(created, started, labels=("anton-oss",), name="unit", completed=None):
    return {
        "id": created,
        "name": name,
        "status": "completed" if completed is not None else "in_progress" if started is not None else "queued",
        "labels": list(labels),
        "created_at": stamp(created),
        "started_at": None if started is None else stamp(started),
        "completed_at": None if completed is None else stamp(completed),
        "runner_name": None if started is None else "runner",
    }


def run(identity, jobs, event="pull_request", attempt=1):
    return {
        "id": identity,
        "run_attempt": attempt,
        "event": event,
        "path": ".github/workflows/test.yml",
        "status": "completed",
        "conclusion": "success",
        "jobs": jobs,
    }


def wall(identity, seconds, event="pull_request"):
    return run(identity, [job(-4000, -3990, name="Gate — Required", completed=-4000 + seconds)], event)


WAITED = run(
    20,
    [
        job(-5000, -4560, ("anton-oss-light",)),
        job(-5000, -4900, ("anton-oss-light",)),
        job(-5000, -4990, ("anton-batch",)),
    ],
)
WINDOW = [wall(10, 952), wall(11, 400), wall(12, 1200), wall(13, 2000, "merge_group"), WAITED]


@pytest.fixture
def actions(env, monkeypatch):  # noqa: F811
    assert doctor.main([WATCHED, "start"]) == 0
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: NOW)
    calls = []

    def read(since, now, *args, **kwargs):
        calls.append((since, now))
        return {"complete": True, "runs": WINDOW, "api_calls": 1, "discovered_runs": len(WINDOW)}

    def attempt(identity, number, *args, **kwargs):
        calls.append((identity, number))
        return next(r for r in WINDOW if r["id"] == identity and r["run_attempt"] == number)

    monkeypatch.setattr("scripts.doctor.ci_speed_read.read", read)
    monkeypatch.setattr("scripts.doctor.ci_speed_read.attempt", attempt, raising=False)
    monkeypatch.setattr(doctor.detect, "readers", lambda *args, **kwargs: {})
    return calls


def lines(capsys):
    return capsys.readouterr().out.splitlines()


def test_wall_time_finding_prints_its_run_value_and_its_tier_over_the_window(actions, capsys):
    capsys.readouterr()
    assert doctor.main([WATCHED, "measure", "ci-tier-wall-time/10-1"]) == 0
    assert lines(capsys) == [
        "ci-tier-wall-time/10-1 952",
        "ci-tier-wall-time pull request, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: "
        "2 of 3 runs over 900 seconds, median 952 seconds, worst 1200 seconds",
    ]
    assert actions == [(10, 1), (NOW - HOUR_MS, NOW)]


def test_runner_wait_finding_prints_its_label_value_and_label_class(actions, capsys):
    capsys.readouterr()
    assert doctor.main([WATCHED, "measure", 'ci-runner-wait/20-1-["anton-oss-light"]']) == 0
    assert lines(capsys) == [
        'ci-runner-wait/20-1-["anton-oss-light"] 440',
        "ci-runner-wait anton-oss-light, window 2026-10-17T21:00:00Z to 2026-10-17T22:00:00Z: "
        "1 of 1 runs over 300 seconds, median 440 seconds, worst 440 seconds",
    ]


def test_bare_class_prints_every_scope_over_the_stated_window(actions, capsys):
    capsys.readouterr()
    flags = ["--since", "2026-10-17T19:00Z", "--until", "2026-10-17T21:00Z"]
    assert doctor.main([WATCHED, "measure", "ci-tier-wall-time", *flags]) == 0
    assert lines(capsys) == [
        "ci-tier-wall-time pull request, window 2026-10-17T19:00:00Z to 2026-10-17T21:00:00Z: "
        "2 of 3 runs over 900 seconds, median 952 seconds, worst 1200 seconds",
        "ci-tier-wall-time queue, window 2026-10-17T19:00:00Z to 2026-10-17T21:00:00Z: "
        "1 of 1 runs over 900 seconds, median 2000 seconds, worst 2000 seconds",
    ]
    assert actions == [(NOW - 3 * HOUR_MS, NOW - HOUR_MS)]
