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


@pytest.mark.parametrize(
    "field,value",
    [
        ("lane", "ci"),
        ("task", "different"),
        ("harness", "claude"),
        ("profile", "qa"),
        ("slot", "other"),
        ("route_kind", "interactive"),
        ("execution_id", "unknown"),
    ],
)
def test_changed_order_identity_is_refused(setup, field, value):
    order = replace(setup.order, **{field: value})
    setup.orders.send("host", order)
    setup.consumer.poll()
    assert setup.orders.ack(order).failure == "identity"
    setup.runtime.spawn.assert_not_called()


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"id":"other"}'])
def test_unusable_prompt_is_refused(setup, raw):
    setup.redis.set("prompt", raw)
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "prompt"
    setup.runtime.spawn.assert_not_called()


def test_inflight_replay_never_publishes_a_failure_over_a_live_launch(setup):
    from scripts.hive.orders import Consumer

    original = setup.runtime.spawn.return_value

    def overlap(request):
        assert Consumer(setup.redis, "host", setup.runtime).poll() == 1
        assert setup.orders.ack(setup.order) is None
        return original

    setup.runtime.spawn.side_effect = overlap
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).status == "live"
    setup.runtime.spawn.assert_called_once()


def test_started_order_expires_at_its_deadline_without_relaunch(setup, monkeypatch):
    from scripts.swarm import lease

    setup.orders.send("host", setup.order)
    setup.redis.hset(setup.orders.receipt(setup.order), "started", str(setup.order.expires_at - 60000))
    monkeypatch.setattr(lease, "now_ms", lambda store: setup.order.expires_at)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "expired"
    setup.runtime.spawn.assert_not_called()


def test_finish_publishes_the_canonical_ack_for_every_delivery(setup):
    from scripts.hive.orders import Ack

    first = setup.orders.send("host", setup.order)
    live = Ack("live", {"host_id": "host"})
    setup.orders.finish("host", first, setup.order, live)
    second = setup.orders.send("host", setup.order)
    setup.orders.finish("host", second, setup.order, Ack("failed", {}, "expired"))
    assert setup.orders.ack(setup.order) == live
    assert json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", second)) == {
        "status": "live",
        "target": {"host_id": "host"},
        "failure": "",
    }


@pytest.mark.parametrize("raw", ["not json", "{}", "[]"])
def test_malformed_delivery_is_acknowledged_and_does_not_block_the_stream(setup, raw):
    item = setup.redis.xadd(setup.orders.stream("host"), {"order": raw})
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", item))
    assert ack["failure"] == "invalid_order"
    assert setup.orders.ack(setup.order).status == "live"


def test_missing_delivery_payload_is_acknowledged(setup):
    item = setup.redis.xadd(setup.orders.stream("host"), {"bad": "payload"})
    setup.consumer.poll()
    assert json.loads(setup.redis.hget(f"{setup.orders.stream('host')}:acks", item))["failure"] == "invalid_order"


@pytest.mark.parametrize("field,value", [("harness", "claude"), ("profile", "qa"), ("account", "other")])
def test_runtime_substitution_is_refused_and_terminated(setup, field, value):
    placed = setup.runtime.spawn.return_value.value
    setup.runtime.spawn.return_value = replace(
        setup.runtime.spawn.return_value, value=replace(placed, **{field: value})
    )
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "identity"
    setup.runtime.terminate.assert_called_once()


def test_launch_that_crosses_expiry_is_terminated(setup, monkeypatch):
    from scripts.swarm import lease

    original = setup.runtime.spawn.return_value

    def delayed(request):
        monkeypatch.setattr(lease, "now_ms", lambda store: setup.order.expires_at)
        return original

    setup.runtime.spawn.side_effect = delayed
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "expired"
    setup.runtime.terminate.assert_called_once()


def test_epoch_takeover_during_launch_refuses_live_admission(setup, monkeypatch):
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    original = setup.runtime.spawn.return_value

    def takeover(request):
        monkeypatch.setattr(lease, "require_epoch", Mock(side_effect=SwarmError("stale")))
        return original

    setup.runtime.spawn.side_effect = takeover
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "refused"
    setup.runtime.terminate.assert_called_once()


@pytest.mark.parametrize("pid,table", [(None, {12: SimpleNamespace(start_time=34)}), (12, {})])
def test_unqualified_process_after_spawn_is_reported_ambiguous(setup, pid, table):
    placed = replace(setup.runtime.spawn.return_value.value, profile_decision={"validation": {"pid": pid}})
    setup.runtime.spawn.return_value = replace(setup.runtime.spawn.return_value, value=placed)
    setup.runtime.table.return_value = table
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    ack = setup.orders.ack(setup.order)
    assert ack.failure == "ambiguous"
    assert ack.target == {"host_id": "host"}
    assert setup.store.execution("fixture", setup.agent.execution_id).state == "launching"


def test_failed_cleanup_is_reported_ambiguous(setup):
    from scripts.swarm_v2.runtime.base import Outcome, Status

    placed = replace(setup.runtime.spawn.return_value.value, profile="qa")
    setup.runtime.spawn.return_value = replace(setup.runtime.spawn.return_value, value=placed)
    setup.runtime.terminate.return_value = Outcome("terminate", Status.REFUSED, "local")
    setup.orders.send("host", setup.order)
    setup.consumer.poll()
    assert setup.orders.ack(setup.order).failure == "ambiguous"
    setup.runtime.terminate.assert_called_once()
