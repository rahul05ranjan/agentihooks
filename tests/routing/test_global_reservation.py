from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scripts.routing import reservation

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    return now


@pytest.fixture
def redis(clock):
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def test_concurrent_reservations_at_cap_one_have_one_winner(redis):
    slots = reservation.LaunchSlots(redis)
    ready = Barrier(2)

    def take(execution):
        ready.wait()
        return slots.reserve("claude:one", execution, 1, 1000)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(take, ["first", "second"]))
    assert sorted(results) == [False, True]
    assert slots.counts() == {"claude:one": 1}


def test_expiry_and_exit_release_capacity(redis, clock):
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert not slots.reserve("claude:one", "second", 1, 1000)
    clock[0] += 1.001
    assert slots.counts() == {}
    assert slots.reserve("claude:one", "second", 1, 1000)
    assert slots.release("second")
    assert not slots.release("second")
    assert slots.counts() == {}
    assert slots.reserve("claude:one", "third", 1, 1000)
