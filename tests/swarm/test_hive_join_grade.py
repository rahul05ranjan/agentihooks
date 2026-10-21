from dataclasses import replace

import pytest


def test_hive_launch_uses_report_and_keeps_brain_optional():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", state="running"))
    code = store.ensure_code("sw").code
    agent = AgentRecord(f"engineer@{code}-0001", "eng", "task", execution_id="execution", hive="worker")
    checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
    checks["brain"] = False
    join_report.start(store.redis, "sw", agent, "session", checks, {}, 1000)
    found = launch_check.misses(store, "sw", agent, {}, {}, True, ["brain"])
    assert found == {"hive_brain": {"expected": "available", "actual": "missing"}}
    assert set(found) <= launch_check.REPORT_ONLY
    report = launch_check.record(store, "sw", agent, found, 2000, 1000)
    assert report["state"] == "reported"


@pytest.mark.parametrize("missing", [None, "ledger_write", "settings", "pane"])
def test_hive_missing_required_evidence_is_enforced(missing):
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", state="running"))
    code = store.ensure_code("sw").code
    agent = AgentRecord(f"engineer@{code}-0001", "eng", "task", execution_id="execution", hive="worker")
    if missing:
        checks = dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)
        checks[missing] = False
        join_report.start(store.redis, "sw", agent, "session", checks, {}, 1000)
    found = launch_check.misses(store, "sw", agent, {}, {}, True, [])
    assert set(found) == {f"hive_{missing}" if missing else "hive_join"}
    assert not set(found) <= launch_check.REPORT_ONLY
    assert join_report.read(store.redis, replace(agent, generation=3)) == {}
