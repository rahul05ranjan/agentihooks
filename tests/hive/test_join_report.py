import json
from dataclasses import replace

import pytest


def passing():
    from scripts.hive import join_report

    return {name: True for name in (*join_report.REQUIRED, *join_report.OPTIONAL)}


@pytest.mark.parametrize(
    ("missing", "grade"),
    [((), "complete"), (("brain",), "partial"), (("ledger_write",), "failed"), (("brain", "pane"), "failed")],
)
def test_grade_retains_every_miss(missing, grade):
    from scripts.hive import join_report

    checks = passing()
    checks.update(dict.fromkeys(missing, False))
    report = join_report.graded({"checks": checks})
    assert report["grade"] == grade
    assert report["misses"] == {name: {"required": name != "brain"} for name in missing}


def test_missing_required_check_cannot_grade_complete():
    from scripts.hive import join_report

    report = join_report.graded({"checks": {"brain": True}})
    assert report["grade"] == "failed"
    assert set(report["misses"]) == set(join_report.REQUIRED)


def test_report_is_execution_scoped_and_exit_is_preserved(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", generation=3, hive="worker")
    report = join_report.start(redis, "swarm", agent, "session", passing(), {"home": "/render"}, 1000)
    assert report["started_at"] == report["heartbeat_at"] == 1000
    assert report["session_id"] == "session"
    assert report["generation"] == 3
    assert report["status"] == "running"
    assert join_report.read(redis, agent) == report
    assert join_report.read(redis, replace(agent, execution_id="next")) == {}
    ended = join_report.end(redis, agent, "session", "logout", "exited", 2000)
    assert ended["exit_reason"] == "logout"
    assert ended["status"] == "exited"
    assert ended["ended_at"] == 2000

    def probe(*args):
        pytest.fail("ended reports must not be probed")

    monkeypatch.setattr(join_report, "cheap_checks", probe)
    join_report.heartbeat(redis, "worker", 500000)
    assert join_report.read(redis, agent) == ended
    assert json.loads(redis.hget(join_report.key("worker"), "execution")) == ended


def test_heartbeat_refreshes_cheap_checks_at_cadence(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    join_report.start(redis, "swarm", agent, "session", passing(), {}, 1000)
    calls = []
    monkeypatch.setattr(
        join_report, "cheap_checks", lambda *args: calls.append(args) or {"pane": False, "brain": False}
    )
    join_report.heartbeat(redis, "worker", 1001)
    assert calls == []
    join_report.heartbeat(redis, "worker", 1000 + join_report.HEARTBEAT_MS)
    assert len(calls) == 1
    report = join_report.read(redis, agent)
    assert report["heartbeat_at"] == 1000 + join_report.HEARTBEAT_MS
    assert report["grade"] == "failed"
    assert report["checks"]["github"] is True
    assert report["misses"] == {"pane": {"required": True}, "brain": {"required": False}}


def test_old_session_cannot_end_current_report():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    redis = FakeRedis(decode_responses=True)
    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", hive="worker")
    report = join_report.start(redis, "swarm", agent, "new", passing(), {}, 1000)
    assert join_report.end(redis, agent, "old", "logout", "exited", 2000) == {}
    assert join_report.read(redis, agent) == report
