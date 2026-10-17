import json
import threading

import pytest

from scripts.inbox import pool
from scripts.inbox.pool import POOLS, MasterPool, live_masters, turn
from scripts.inbox.store import InboxStore, now_ms
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.masters import MasterSeats

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
FRONT = "master@sw"
SECOND = "master-2@sw"
THIRD = "master-3@sw"
POOL = f"{pool.PREFIX}:sw"
CLAIMED = "claimed from the master pool by {}"


def _client(server):
    import fakeredis

    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def swarm(server):
    store = RedisStore(_client(server))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    return store


@pytest.fixture
def inbox(swarm):
    return InboxStore(swarm.redis, session_id="s1")


def _masters(swarm, count, states=None):
    states = states or {}
    for index in range(1, count + 1):
        seat = FRONT if index == 1 else f"master-{index}@{SLUG}"
        name = f"sw-master-{index}"
        swarm.put_agent(SLUG, AgentRecord(name, "master", "master", seat=seat, state=states.get(name, "working")))
        swarm.seats.occupy(seat, name, at=1)


@pytest.fixture
def held(monkeypatch):
    """Items sent meanwhile wait in the pool; undo hands distribution back."""
    monkeypatch.setattr(MasterPool, "distribute", lambda self, slug, live=None: [])
    return monkeypatch


def test_a_single_master_gets_its_items_exactly_as_before(swarm, inbox):
    _masters(swarm, 1)
    item = inbox.send("swarm", FRONT, "check the queue")
    assert swarm.redis.exists(POOL) == 0
    assert swarm.redis.smembers(POOLS) == set()
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [item.id]
    assert inbox.deliver(item.id, "sw-master-1").state == "delivered"
    assert [inbox.get(i.id).address for i in inbox.redeliver(now_ms() + 10, 0)] == [FRONT]
    assert [(e["state"], e["reason"]) for e in inbox.history(item.id)] == [
        ("pending", ""),
        ("delivered", ""),
        ("pending", "redelivered: never confirmed inside the redelivery window"),
    ]
    assert inbox.get(item.id).address == FRONT
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [item.id]


def test_items_go_round_robin_across_masters_in_arrival_order(swarm, inbox):
    _masters(swarm, 3)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(6)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, SECOND, THIRD, FRONT, SECOND, THIRD]
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [items[0].id, items[3].id]
    assert [i.id for i in inbox.pending_mail("sw-master-2")] == [items[1].id, items[4].id]
    assert [i.id for i in inbox.pending_mail("sw-master-3")] == [items[2].id, items[5].id]
    assert inbox.get(items[1].id).reason == CLAIMED.format(SECOND)
    assert [i.id for i in inbox.open_items(FRONT)] == [items[0].id, items[3].id]
    assert [i.id for i in inbox.open_items(SECOND)] == [items[1].id, items[4].id]
    assert swarm.redis.get(f"{POOL}:last") == THIRD
    assert swarm.redis.exists(POOL) == 0
    assert swarm.redis.smembers(POOLS) == set()


def test_waiting_items_leave_the_pool_oldest_first(swarm, inbox, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(4)]
    assert swarm.redis.zrange(POOL, 0, -1) == [i.id for i in items]
    assert swarm.redis.smembers(POOLS) == {SLUG}
    held.undo()
    claimed = inbox.pool.distribute(SLUG)
    assert [(i.id, i.address) for i in claimed] == [
        (items[0].id, FRONT),
        (items[1].id, SECOND),
        (items[2].id, FRONT),
        (items[3].id, SECOND),
    ]
    assert inbox.pool.distribute(SLUG) == []
    assert swarm.redis.smembers(POOLS) == set()


def test_no_master_takes_an_item_still_in_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "who takes this")
    assert inbox.pending_mail("sw-master-1") == []
    assert inbox.pending_items(FRONT) == []
    assert inbox.deliver(item.id, "sw-master-1") is None
    assert inbox.get(item.id).state == "pending"


def test_a_closed_item_drops_out_of_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    gone, kept = (inbox.send("swarm", FRONT, text) for text in ("gone", "kept"))
    inbox.withdraw(gone.id, "swarm", "cancelled: no longer needed")
    held.undo()
    assert [(i.id, i.address) for i in inbox.pool.distribute(SLUG)] == [(kept.id, FRONT)]
    assert swarm.redis.exists(POOL) == 0


def test_operator_items_bypass_the_pool_to_the_front_door(swarm, inbox):
    _masters(swarm, 2)
    items = [inbox.send("operator", FRONT, f"On ledger sw: line {n}") for n in range(3)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, FRONT, FRONT]
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [i.id for i in items]
    assert inbox.pending_mail("sw-master-2") == []
    assert swarm.redis.exists(f"{POOL}:last") == 0
    assert all(e["state"] == "pending" and e["reason"] == "" for i in items for e in inbox.history(i.id))


def test_masters_gone_or_draining_are_passed_over(swarm, inbox):
    _masters(swarm, 6, {"sw-master-2": "finished", "sw-master-4": "retiring"})
    swarm.redis.hset(swarm.key(SLUG, "master-retire-requests"), "sw-master-3", json.dumps({"reason": "shrank"}))
    MasterSeats(swarm.redis).retire(SLUG, "master-5@sw")
    assert live_masters(swarm.redis, SLUG) == [FRONT, "master-6@sw"]
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(3)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, "master-6@sw", FRONT]


def test_a_master_record_without_a_seat_holds_the_front_door(swarm):
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master"))
    swarm.put_agent(SLUG, AgentRecord("sw-master-10", "master", "master", seat="master-10@sw"))
    swarm.put_agent(SLUG, AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw"))
    assert live_masters(swarm.redis, SLUG) == [FRONT, "master-10@sw"]


def test_turn_follows_seat_order_and_passes_over_the_last_holder():
    seats = [FRONT, SECOND, "master-10@sw"]
    assert turn(seats, "", "") == FRONT
    assert turn(seats, FRONT, "") == SECOND
    assert turn(seats, SECOND, "") == "master-10@sw"
    assert turn(seats, "master-10@sw", "") == FRONT
    assert turn(seats, "master-10@sw", FRONT) == SECOND
    assert turn([FRONT, SECOND], SECOND, SECOND) == FRONT
    assert turn([SECOND], SECOND, SECOND) == SECOND
    assert turn(seats, THIRD, "") == "master-10@sw"


def test_only_front_door_items_from_others_are_pooled(swarm, inbox):
    _masters(swarm, 2)
    assert inbox.pool.masters_for("swarm", FRONT) == [FRONT, SECOND]
    assert inbox.pool.masters_for("operator", FRONT) == []
    assert inbox.pool.masters_for("swarm", SECOND) == []
    assert inbox.pool.masters_for("swarm", "eng-1@sw") == []
    assert inbox.pool.masters_for("swarm", "sw-eng-1") == []
    assert pool.slug_of(FRONT) == SLUG
    assert pool.slug_of(SECOND) == ""


def test_an_unconfirmed_item_goes_to_the_next_master_with_the_same_id(swarm, inbox):
    _masters(swarm, 2)
    first, second = (inbox.send("swarm", FRONT, text) for text in ("first", "second"))
    assert (inbox.get(first.id).address, inbox.get(second.id).address) == (FRONT, SECOND)
    assert inbox.deliver(first.id, "sw-master-1").state == "delivered"
    assert [i.id for i in inbox.redeliver(now_ms() + 10, 0)] == [first.id]
    moved = inbox.get(first.id)
    assert (moved.address, moved.state, moved.reason) == (SECOND, "pending", CLAIMED.format(SECOND))
    assert [i.id for i in inbox.pending_mail("sw-master-2")] == [first.id, second.id]
    assert inbox.pending_mail("sw-master-1") == []
    assert inbox.open_items(FRONT) == []
    assert inbox.deliver(first.id, "sw-master-2").state == "delivered"
    assert [(e["state"], e["by"]) for e in inbox.history(first.id)] == [
        ("pending", "swarm"),
        ("pending", "inbox"),
        ("delivered", "sw-master-1"),
        ("pending", "inbox"),
        ("pending", "inbox"),
        ("delivered", "sw-master-2"),
    ]


def test_an_item_a_backend_master_left_unconfirmed_returns_to_the_front_door(swarm, inbox):
    _masters(swarm, 2)
    first, second = (inbox.send("swarm", FRONT, text) for text in ("first", "second"))
    assert inbox.deliver(first.id, "sw-master-1").state == "delivered"
    assert inbox.deliver(second.id, "sw-master-2").state == "delivered"
    assert inbox.confirm(first.id, "sw-master-1").state == "confirmed"
    assert [i.address for i in inbox.redeliver(now_ms() + 10, 0)] == [FRONT]
    assert inbox.get(second.id).address == FRONT
    assert inbox.open_items(SECOND) == []
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [second.id]
    assert int(swarm.redis.get(inbox.key("open-size", SECOND))) == 0
    assert int(swarm.redis.get(inbox.key("open-size", FRONT))) == 2


def test_a_lone_live_master_takes_what_waits(swarm, inbox, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(2)]
    swarm.put_agent(SLUG, AgentRecord("sw-master-2", "master", "master", seat=SECOND, state="finished"))
    held.undo()
    inbox.redeliver(now_ms(), 1000)
    assert [inbox.get(i.id).address for i in items] == [FRONT, FRONT]
    assert swarm.redis.smembers(POOLS) == set()


def test_items_wait_while_no_master_is_live(swarm, inbox, held):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "wait")
    for name, seat in (("sw-master-1", FRONT), ("sw-master-2", SECOND)):
        swarm.put_agent(SLUG, AgentRecord(name, "master", "master", seat=seat, state="finished"))
    held.undo()
    assert inbox.pool.distribute(SLUG) == []
    assert swarm.redis.zrange(POOL, 0, -1) == [item.id]


def test_swarm_keys_carry_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    inbox.send("swarm", FRONT, "wait")
    keys, _ = inbox.keys_for(lambda address: address.endswith("@sw"))
    assert POOL in keys
    held.undo()
    inbox.pool.distribute(SLUG)
    keys, _ = inbox.keys_for(lambda address: address.endswith("@sw"))
    assert POOL not in keys and f"{POOL}:last" in keys


def test_two_concurrent_claimers_take_each_item_exactly_once(swarm, inbox, server, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(40)]
    held.undo()
    claimers = [(InboxStore(_client(server), session_id=f"s{n}"), f"sw-master-{n}") for n in (1, 2)]
    start = threading.Barrier(len(claimers))

    def claim(store, name):
        start.wait()
        for _ in range(20):
            store.pool.distribute(SLUG)
            for item in store.pending_mail(name):
                store.deliver(item.id, name)

    threads = [threading.Thread(target=claim, args=claimer) for claimer in claimers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for store, name in claimers:
        for item in store.pending_mail(name):
            store.deliver(item.id, name)
    holders = {}
    for item in items:
        history = inbox.history(item.id)
        claims = [e for e in history if e["reason"].startswith("claimed from the master pool")]
        delivered = [e["by"] for e in history if e["state"] == "delivered"]
        assert len(claims) == 1 and len(delivered) == 1
        holders[item.id] = inbox.get(item.id).address
        assert delivered == [swarm.seats.occupant(holders[item.id]).occupant]
    assert sorted(holders.values()) == [FRONT] * 20 + [SECOND] * 20
    assert swarm.redis.exists(POOL) == 0
