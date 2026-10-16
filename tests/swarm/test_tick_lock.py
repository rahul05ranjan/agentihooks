import json
import multiprocessing

import pytest

from scripts.swarm import controller, lease
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def locked_store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(store, "sw", "host")
    return store, held


def hold_tick(store, held, sender):
    sender.send(controller.take_tick_lock(store, "sw", held, 600_000))
    sender.recv()


def test_a_killed_tick_holder_is_replaced_without_waiting_for_expiry(locked_store):
    store, held = locked_store
    receiver, sender = multiprocessing.Pipe()
    child = multiprocessing.get_context("fork").Process(target=hold_tick, args=(store, held, sender))
    child.start()
    try:
        assert receiver.poll(10)
        original = receiver.recv()
        store.redis.set(store.key("sw", "tick-lock"), original, px=600_000)
        assert controller.take_tick_lock(store, "sw", held, 600_000) is None
        child.kill()
        child.join(10)
        assert child.exitcode == -9
        assert store.redis.pttl(store.key("sw", "tick-lock")) > 590_000
        replacement = controller.take_tick_lock(store, "sw", held, 600_000)
        assert replacement is not None and replacement != original
        controller.release_tick_lock(store, "sw", original)
        assert store.redis.get(store.key("sw", "tick-lock")) == replacement
        with pytest.raises(SwarmError, match="tick lock is stale"):
            controller.keep_tick(store, "sw", held, original, 600_000)
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)
        receiver.close()
        sender.close()


def test_a_live_tick_holder_keeps_its_lock(locked_store):
    store, held = locked_store
    original = controller.take_tick_lock(store, "sw", held, 600_000)
    assert original is not None
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original


def test_a_stale_controller_cannot_replace_or_renew_the_current_lock(locked_store):
    store, held = locked_store
    original = controller.take_tick_lock(store, "sw", held, 600_000)
    assert lease.release(store, "sw", held)
    current = lease.acquire(store, "sw", "successor")
    replacement = controller.take_tick_lock(store, "sw", current, 600_000)
    assert replacement is not None
    with pytest.raises(SwarmError, match="stale"):
        controller.take_tick_lock(store, "sw", held, 600_000)
    controller.release_tick_lock(store, "sw", original)
    assert store.redis.get(store.key("sw", "tick-lock")) == replacement


@pytest.mark.parametrize("original", ["legacy", json.dumps({"epoch": 1, "token": "legacy"})])
def test_a_lock_without_a_verifiable_holder_is_not_stolen(locked_store, original):
    store, held = locked_store
    store.redis.set(store.key("sw", "tick-lock"), original, px=600_000)
    assert controller.take_tick_lock(store, "sw", held, 600_000) is None
    assert store.redis.get(store.key("sw", "tick-lock")) == original
