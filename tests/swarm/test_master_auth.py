from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("five_left,week_left,cap", [(90, 25, 6), (90, 9, 6), (4, 25, 0), (90, 25, None), (90, 25, 0)])
def test_only_the_master_can_use_reserved_room(tmp_path, lane, five_left, week_left, cap):
    from scripts.routing.reservation import Reservation
    from scripts.swarm.capacity import Account, reserve
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import SpawnError
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        out = "status=started\nroute_status=routed\naccount=reserved\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    state = "UNKNOWN" if cap is None else "OPEN" if cap else "CLOSED"
    runtime._quota_accounts = reserve(
        [Account("claude", "reserved", state, 0, five_left, week_left, cap)],
        Reservation("claude", "reserved", 1),
    )
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": lane, "title": "proof", "route_kind": "subscription"}
    if lane != "master" or five_left == 4 or week_left == 9 or not cap:
        with pytest.raises(SpawnError):
            runtime.spawn(config, lane, "agent", task)
        assert not seen
        return
    placed = runtime.spawn(config, lane, "agent", task)
    passed = seen[0][seen[0].index("--") + 1 :]
    assert passed[:2] == ["--route", "reserved"]
    assert placed.account == "reserved"
    assert placed.route_kind == "subscription"
    assert runtime._quota_accounts[0].sessions == 1
    assert runtime._quota_accounts[0].cap == 0


@pytest.mark.parametrize("login", [True, False])
@pytest.mark.parametrize("handoff", [True, False])
def test_master_prefers_a_valid_login_even_after_a_token_handoff(tmp_path, login, handoff):
    from scripts.routing import native_login
    from scripts.swarm.capacity import Account
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=0 if login else 1, stdout=native_login.STATUS if login else "", stderr="")
        seen.append(argv)
        out = "status=started\nroute_status=routed\naccount=token\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [Account("claude", "token", "OPEN", 0, 90, 90, 6)]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof"}
    if handoff:
        task["handoff"] = "Continue the master task."
        task["handoff_envelope"] = {
            "launch": {"profile": "master", "harness": "claude", "account": "token", "model": "opus", "effort": "high"}
        }
    placed = runtime.spawn(config, "master", "agent", task)
    passed = seen[0][seen[0].index("--") + 1 :]
    assert passed[:2] == ["--route", "interactive" if login else "token"]
    assert placed.route_kind == ("interactive" if login else "subscription")


@pytest.mark.parametrize("kind", ["subscription", "api", "interactive"])
@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("desired", ["interactive", ""])
def test_health_reports_only_master_fallback_and_clears_after_move_back(kind, lane, desired):
    from scripts.swarm.health.findings import Limits, findings

    agent = {
        "name": "agent",
        "seat": "master@proof",
        "lane": lane,
        "task": lane,
        "state": "working",
        "route_kind": kind,
        "desired_route_kind": desired,
    }
    found = [f for f in findings({}, [agent], {}, Limits()) if f.kind == "master-fallback-auth"]
    if lane == "master" and desired == "interactive" and kind != "interactive":
        assert len(found) == 1
        assert found[0].id == "master-fallback-auth/master@proof"
        assert found[0].summary == "master on fallback auth, phone access lost"
        assert found[0].measure == 1
        assert f"route: {kind}" in found[0].evidence
    else:
        assert found == []


def test_finished_fallback_master_raises_no_finding_and_unseated_master_uses_its_name():
    from scripts.swarm.health.findings import Limits, findings

    agent = {
        "name": "master-agent",
        "lane": "master",
        "task": "master",
        "state": "working",
        "route_kind": "subscription",
        "desired_route_kind": "interactive",
    }
    found = [f for f in findings({}, [agent], {}, Limits()) if f.kind == "master-fallback-auth"]
    assert [f.id for f in found] == ["master-fallback-auth/master-agent"]
    agent["state"] = "finished"
    assert not [f for f in findings({}, [agent], {}, Limits()) if f.kind == "master-fallback-auth"]


def test_desired_interactive_route_survives_storage_and_legacy_records_default_empty():
    import json

    import fakeredis

    from scripts.swarm.store import AgentRecord, RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    record = AgentRecord("agent", "master", "master", route_kind="subscription", desired_route_kind="interactive")
    store.put_agent("proof", record)
    assert store.agents("proof")[0].desired_route_kind == "interactive"
    raw = json.loads(store.redis.hget(store.key("proof", "agents"), "agent"))
    del raw["desired_route_kind"]
    store.redis.hset(store.key("proof", "agents"), "agent", json.dumps(raw))
    assert store.agents("proof")[0].desired_route_kind == ""
