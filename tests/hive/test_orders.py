import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def setup():
    import fakeredis

    from scripts.hive.orders import Consumer, LaunchOrder, Orders
    from scripts.swarm import lease
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    held = lease.acquire(store, "fixture", "controller")
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            state="launching",
            harness="codex",
            profile="engineer",
            account="account",
            hive="host",
            runtime_target={"host_id": "host"},
        ),
    )
    redis.set("prompt", json.dumps({"id": "task", "description": "launch this task"}))
    order = LaunchOrder(
        "fixture",
        agent.execution_id,
        agent.generation,
        "engineer",
        "eng",
        "task",
        "account",
        "codex",
        "subscription",
        "prompt",
        held.epoch,
        lease.now_ms(store) + 60000,
    )
    placed = Placed("pane", "codex", "account", profile="engineer", profile_decision={"validation": {"pid": 12}})
    runtime = Mock()
    runtime.spawn.return_value = Outcome("spawn", Status.OK, "local", placed)
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    return SimpleNamespace(
        redis=redis,
        store=store,
        held=held,
        agent=agent,
        order=order,
        runtime=runtime,
        orders=Orders(redis),
        consumer=Consumer(redis, "host", runtime),
    )


def test_order_ack_and_live_execution(setup):
    item = setup.orders.send("host", setup.order)
    assert setup.redis.xrange(setup.orders.stream("host"))[0][0] == item
    assert setup.consumer.poll() == 1
    ack = setup.orders.ack(setup.order)
    assert ack.status == "live"
    assert ack.failure == ""
    assert ack.target == {"host_id": "host", "process_namespace": "namespace", "pid": 12, "pid_start": 34}
    live = setup.store.execution("fixture", setup.agent.execution_id)
    assert live.generation == setup.agent.generation
    assert live.pane_id == "pane"
    assert live.state == "working"
    assert live.runtime_target == ack.target
    request = setup.runtime.spawn.call_args.args[0]
    assert request.name == setup.agent.name
    assert request.lane == "eng"
    assert request.task["description"] == "launch this task"
    assert request.task["controller_epoch"] == setup.held.epoch
    assert request.task["route_kind"] == "subscription"
    assert request.config.lanes["eng"]["agent"] == "codex"
    assert not setup.redis.xrange(setup.orders.stream("host"))


@pytest.mark.parametrize(
    "field,value,failure", [("epoch", 0, "stale_epoch"), ("expires_at", 1, "expired"), ("generation", 9, "identity")]
)
def test_refusal_never_launches(setup, field, value, failure):
    order = replace(setup.order, **{field: value})
    setup.orders.send("host", order)
    assert setup.consumer.poll() == 1
    ack = setup.orders.ack(order)
    assert ack.status == "failed"
    assert ack.failure == failure
    assert ack.target == {}
    setup.runtime.spawn.assert_not_called()


def test_replay_returns_the_same_ack_without_another_launch(setup):
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order) == ack
    setup.runtime.spawn.assert_called_once()


def test_crashed_launch_is_ambiguous_and_never_relaunched(setup):
    setup.runtime.spawn.side_effect = KeyboardInterrupt
    setup.orders.send("host", setup.order)
    with pytest.raises(KeyboardInterrupt):
        setup.consumer.poll()
    setup.runtime.spawn.side_effect = None
    setup.redis.hset(setup.orders.receipt(setup.order), "started", "1")
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "ambiguous"
    setup.runtime.spawn.assert_called_once()


def test_wrong_host_cannot_admit_execution(setup):
    from scripts.hive.orders import Consumer

    setup.orders.send("other", setup.order)
    Consumer(setup.redis, "other", setup.runtime).poll()
    assert setup.orders.ack(setup.order).failure == "identity"
    setup.runtime.spawn.assert_not_called()


def test_runtime_failure_is_typed(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    setup.runtime.spawn.return_value = Outcome("spawn", Status.UNAVAILABLE, "local", detail="no launch slot")
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "unavailable"


def test_missing_prompt_is_refused(setup):
    setup.redis.delete("prompt")
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "prompt"
    setup.runtime.spawn.assert_not_called()


def test_host_identity_is_immutable_after_process_binding(setup):
    from scripts.swarm.store import SwarmError

    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    live = setup.store.execution("fixture", setup.agent.execution_id)
    with pytest.raises(SwarmError, match="immutable"):
        setup.store.put_agent("fixture", replace(live, runtime_target={**live.runtime_target, "host_id": "other"}))


def test_daemon_consumes_orders_after_its_heartbeat(setup, monkeypatch):
    from scripts.hive import daemon
    from scripts.hive.orders import Consumer

    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": {}, "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(Consumer, "local", classmethod(lambda cls, redis, hive: setup.consumer))
    setup.orders.send("host", setup.order)
    daemon.beat(setup.redis, "host")
    assert setup.orders.ack(setup.order).status == "live"
