from dataclasses import replace

import pytest


def test_hive_launch_uses_report_and_keeps_brain_optional():
    from fakeredis import FakeRedis

    from scripts.hive import join_report
    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
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
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, state="running"))
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


@pytest.mark.parametrize(("relaunch", "spent"), [(False, False), (True, True)])
@pytest.mark.parametrize("lane", ["eng", "master"])
def test_required_hive_failure_retires_without_relaunch_entitlement(monkeypatch, relaunch, spent, lane):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord(store.next_name("sw", lane), lane, "task", hive="worker", started_at=1000)
    launch_check.begin(store, "sw", agent, 1000, relaunch=relaunch)
    if spent:
        launch_check.mark_relaunched(store, "sw", "task")
    retired, notices = [], []
    runtime = SimpleNamespace(
        live_names=lambda: {agent.name}, retire=lambda agent, **kwargs: retired.append(agent.name) or True
    )
    ledger = SimpleNamespace(notify=lambda *args: notices.append(args))
    monkeypatch.setattr(controller, "_drop", lambda *args: " dropped")
    found = {"hive_settings": {"expected": "available", "actual": "missing"}}
    action = controller._failed_launch(
        "sw", store, ledger, runtime, {}, launch_check.Miss(agent, found, relaunch), 10000
    )
    assert retired == [agent.name]
    assert action == f"retired {agent.name} after its hive join failed dropped"
    assert launch_check.report(store, "sw", "task")["state"] == "failed"
    assert agent.name not in launch_check.pending(store, "sw")
    assert notices == (
        [("sw", "The master failed its launch check within a minute on settings. The failed hive agent was retired.")]
        if lane == "master"
        else []
    )


def test_optional_hive_miss_settles_admission_and_never_retires(monkeypatch):
    import importlib
    from types import SimpleNamespace

    from fakeredis import FakeRedis

    from scripts.swarm import launch_check
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    controller = importlib.import_module("scripts.swarm.tick")
    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", hive="worker", started_at=1000)
    settled = []
    monkeypatch.setattr(controller, "_settle_admission", lambda *args: settled.append(args))
    found = {"hive_brain": {"expected": "available", "actual": "missing"}}
    action = controller._failed_launch(
        "sw", store, SimpleNamespace(), SimpleNamespace(), {}, launch_check.Miss(agent, found, True), 10000
    )
    assert "reported only" in action
    assert settled == [("sw", settled[0][1], agent)]
    assert launch_check.report(store, "sw", "task")["state"] == "reported"
