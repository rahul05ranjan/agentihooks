from datetime import datetime, timezone

import pytest

from scripts.doctor import ci_speed

pytestmark = pytest.mark.unit
NOW = 1792274400000


def stamp(seconds):
    return datetime.fromtimestamp((NOW + seconds * 1000) / 1000, timezone.utc).isoformat()


def job(wait, *, queued=False, created=-1000, name="unit", identity=11):
    return {
        "id": identity,
        "name": name,
        "created_at": stamp(created),
        "started_at": None if queued else stamp(created + wait),
        "completed_at": None,
        "status": "queued" if queued else "in_progress",
        "labels": ["self-hosted", "anton-oss"],
    }


def record(jobs, event="push", identity=7, **extra):
    return {
        "id": identity,
        "event": event,
        "path": ".github/workflows/test.yml",
        "status": "in_progress",
        "jobs": jobs,
        **extra,
    }


@pytest.mark.parametrize("wait, count", [(299, 0), (300, 0), (301, 1)])
def test_runner_wait_crosses_only_above_five_minutes(wait, count):
    found = ci_speed.findings([record([job(wait)])], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        [finding] = found
        assert finding.kind == "ci runner wait"
        assert finding.measure == 301
        assert all(text in " ".join(finding.evidence) for text in ["anton-oss", "unit", "7", "301"])


def test_a_still_queued_job_uses_now_and_remains_visible_after_its_creation_window():
    [finding] = ci_speed.findings([record([job(0, queued=True, created=-301)])], NOW - 60000, NOW)
    assert finding.kind == "ci runner wait" and finding.measure == 301


@pytest.mark.parametrize("status", ["queued", "pending", "waiting"])
def test_unstarted_jobs_ignore_placeholder_start_timestamps(status):
    waiting = job(0, queued=True, created=-301)
    waiting.update(status=status, started_at=waiting["created_at"])
    [finding] = ci_speed.findings([record([waiting])], NOW - 60000, NOW)
    assert finding.measure == 301


def test_a_previously_started_job_outside_the_pass_window_is_not_counted_again():
    assert ci_speed.findings([record([job(301)])], NOW - 60000, NOW) == []


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("elapsed, count", [(899, 0), (900, 0), (901, 1)])
def test_tier_wall_includes_runner_wait_and_crosses_only_above_fifteen_minutes(event, elapsed, count):
    first = job(0, created=-elapsed)
    gate = job(0, created=-10, name="Gate — Required", identity=12)
    gate.update(status="completed", completed_at=stamp(0))
    found = ci_speed.findings([record([first, gate], event, status="completed")], NOW - 2000000, NOW)
    assert len(found) == count
    if count:
        assert found[0].kind == "ci tier wall time" and found[0].measure == 901
        assert event in " ".join(found[0].evidence)


def test_completed_pr_wall_stops_at_gate_result_and_promotion_has_no_budget():
    first = job(0, created=-1200)
    gate = job(0, created=-400, name="Gate — Required", identity=12)
    gate.update(status="completed", completed_at=stamp(-300))
    assert ci_speed.findings([record([first, gate], "pull_request")], NOW - 2000000, NOW) == []
    assert ci_speed.findings([record([first], "push")], NOW - 2000000, NOW) == []


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_an_unfinished_tier_uses_now(event):
    [finding] = ci_speed.findings([record([job(0, created=-901)], event)], NOW - 2000000, NOW)
    assert finding.measure == 901


def test_every_crossing_is_reported_without_a_display_limit():
    jobs = [job(301, identity=n) for n in range(150)]
    assert len(ci_speed.findings([record(jobs)], NOW - 2000000, NOW)) == 150
