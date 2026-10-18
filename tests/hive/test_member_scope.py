import json

import pytest

from scripts.swarm.keyspace import ROOT

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

MEMBER_URL = "rediss://hive.example:6380/0"
COMPOSE = {"AGENTIHOOKS_DEPLOYMENT": "compose"}


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def swarm(redis):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(redis)
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def place(store, hive, seat="eng-1@fixture"):
    from scripts.swarm.store import AgentRecord

    name = store.next_name("fixture", "eng")
    store.start_execution("fixture", AgentRecord(name, "eng", "task", seat=seat, hive=hive))
    store.seats.occupy(seat, name, 1)
    return name


def join(redis, member):
    from scripts.hive import auth

    redis.hset(f"{auth.PREFIX}:member:{member}", mapping={"name": member, "ledger": "digest"})


def as_member(monkeypatch, member="m1"):
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", member)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", MEMBER_URL)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({**COMPOSE, "AGENTIHOOKS_HIVE_ID": "m1", "AGENTIHOOKS_HIVE_REDIS_URL": MEMBER_URL}, "m1"),
        (
            {"AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_HIVE_ID": "m1", "AGENTIHOOKS_HIVE_REDIS_URL": MEMBER_URL},
            "",
        ),
        ({**COMPOSE, "AGENTIHOOKS_HIVE_REDIS_URL": MEMBER_URL}, ""),
        ({**COMPOSE, "AGENTIHOOKS_HIVE_ID": "m1"}, ""),
    ],
)
def test_a_process_is_a_member_only_while_it_talks_redis_as_that_member(environ, expected):
    from scripts.hive import scope

    assert scope.member(environ) == expected


def test_a_member_beat_lands_below_its_own_hive_key_and_the_controller_reads_it_there(redis, swarm, monkeypatch):
    from scripts.swarm import idle

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        idle.beat(redis, "fixture", name, "working", 5)
        idle.prompted(redis, "fixture", name, 6)
        idle.declare_wait(redis, "fixture", name, 100, "checks", 7)
        assert idle.heartbeat(redis, "fixture", name) == {"state": "working", "at": 5}

    assert redis.exists(f"{ROOT}:hive:m1:swarm:fixture:heartbeat:{name}")
    assert not redis.keys(f"{ROOT}:swarm:fixture:*:{name}")
    assert idle.heartbeat(redis, "fixture", name) == {"state": "working", "at": 5}
    assert idle.last_prompt(redis, "fixture", name) == 6
    assert idle.wait(redis, "fixture", name)["reason"] == "checks"
    assert idle.waited(redis, "fixture", name) == 100
    assert [entry["at"] for entry in idle.wait_history(redis, "fixture", name)] == [7]


@pytest.mark.parametrize("hive", ["", "other"])
def test_an_agent_on_the_controller_or_an_unjoined_hive_keeps_the_shared_readings(redis, swarm, hive):
    from scripts.swarm import idle

    name = place(swarm, hive)
    idle.beat(redis, "fixture", name, "idle", 9)

    assert json.loads(redis.get(f"{ROOT}:swarm:fixture:heartbeat:{name}")) == {"state": "idle", "at": 9}
    assert idle.heartbeat(redis, "fixture", name) == {"state": "idle", "at": 9}


def test_member_session_model_progress_and_stop_blocks_stay_below_the_member(redis, swarm, monkeypatch):
    from scripts.gates.claim_stop import Streak
    from scripts.gates.progress import Progress
    from scripts.swarm import session_model

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        session_model.put(redis, "fixture", name, "opus", "high", 3)
        Progress(redis, "fixture").outcome(name, "pushed", 4)
        assert Streak(swarm, "fixture", name).bump(5) == 1

    own = f"{ROOT}:hive:m1:swarm:fixture"
    assert sorted(redis.keys(f"{own}:*")) == sorted(
        f"{own}:{kind}:{name}" for kind in ("session-model", "progress", "stop-blocks")
    )
    assert session_model.get(redis, "fixture", name)["model"] == "opus"
    assert Progress(redis, "fixture").read(name).outcome == "pushed"


def test_a_member_inbox_keeps_its_items_owners_and_marks_below_its_own_hive_key(redis, swarm, monkeypatch):
    from scripts.inbox.dispatch import Dispatcher
    from scripts.inbox.seen import SeenMarks
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    InboxStore(redis).send("operator", name, "hello")
    assert not redis.keys(f"{ROOT}:inbox:*")
    with monkeypatch.context() as member:
        as_member(member)
        inbox = InboxStore(redis)
        item = inbox.pending_mail(name)[0]
        inbox.read(item.id, name)
        assert SeenMarks(redis).mark(name, "fixture:1:note")
        Dispatcher(inbox).own(name, "session")

    assert not redis.keys(f"{ROOT}:inbox:*")
    assert redis.hget(f"{ROOT}:hive:m1:inbox:item:{item.id}", "state") == "read"
    assert redis.exists(f"{ROOT}:hive:m1:inbox:seen:{name}")
    assert redis.get(f"{ROOT}:hive:m1:inbox:owner:{name}") == "session"


def test_a_member_send_waits_in_its_own_keys_until_the_controller_relays_it(redis, swarm, monkeypatch):
    from scripts.inbox import relay
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        item = InboxStore(redis).send(name, "master@fixture", "done")
        forged = InboxStore(redis).send("operator", "master@fixture", "forged")
        assert not redis.keys(f"{ROOT}:inbox:*")

    assert relay.relay(redis) == [item.id]
    assert [mail.id for mail in InboxStore(redis).open_items("master@fixture")] == [item.id]
    assert InboxStore(redis).get(item.id).sender == name
    world = InboxStore(redis, world="m1")
    assert world.get(item.id).state == "cancelled"
    assert world.get(item.id).reason == relay.RELAYED
    assert world.get(forged.id).reason == relay.FOREIGN_SENDER
    assert world.pending_items("master@fixture") == []
    assert relay.relay(redis) == []


def test_a_controller_send_to_an_agent_on_a_member_lands_in_that_members_keys(redis, swarm, monkeypatch):
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    item = InboxStore(redis).send("master@fixture", "eng-1@fixture", "next step")

    assert not redis.exists(f"{ROOT}:inbox:item:{item.id}")
    assert redis.exists(f"{ROOT}:hive:m1:inbox:item:{item.id}")
    with monkeypatch.context() as member:
        as_member(member)
        assert [mail.id for mail in InboxStore(redis).pending_mail(name)] == [item.id]


def test_the_controller_refreshes_live_launch_slots_from_every_live_hive_report(redis, monkeypatch):
    from scripts.hive import registry
    from scripts.routing.reservation import LaunchSlots

    monkeypatch.setattr("time.time", lambda: 100.0)
    slots = LaunchSlots(redis)
    for execution in ("live", "stale", "unreported"):
        assert slots.reserve("claude:one", execution, 3, 10000)
        assert slots.acknowledge(execution)
        redis.pexpire(f"{ROOT}:launch-slot:{execution}", 1000)
    for hive, at, executions in (("m1", 100_000, ["live"]), ("gone", 1, ["stale"])):
        redis.sadd(registry.INDEX, hive)
        redis.hset(registry.key(hive), "heartbeat_at", at)
        redis.set(f"{registry.key(hive)}:sessions", json.dumps({"_executions": {"claude:one": executions}}))

    slots.refresh_reported(100_000)

    assert round(redis.pttl(f"{ROOT}:launch-slot:live") / 1000) == 45
    assert round(redis.pttl(f"{ROOT}:launch-slot:stale") / 1000) == 1
    assert round(redis.pttl(f"{ROOT}:launch-slot:unreported") / 1000) == 1
