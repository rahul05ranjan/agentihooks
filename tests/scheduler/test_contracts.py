import re
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def scheduler():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    return Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")


def test_default_clock_and_machine_namespace(monkeypatch):
    import fakeredis

    from scripts.scheduler import core
    from scripts.scheduler.slots import InboxSlot

    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: 100))
    monkeypatch.setattr(core, "socket", SimpleNamespace(gethostname=lambda: "default-machine"))
    redis = fakeredis.FakeRedis(decode_responses=True)
    scheduler = core.Scheduler(redis)
    timer = scheduler.timer(owner="operator", payload="notice", delay=0)
    timer.timeout.connect(InboxSlot("proof-session"))
    timer.start()
    assert timer.record["kind"] == "timer"
    assert timer.record["started_at"] == 100
    assert timer.record["due"] == 100
    assert timer.record["owner"] == "operator"
    assert timer.record["payload"] == "notice"
    assert core.Scheduler(redis, machine="other-machine").run_due() == 0
    assert scheduler.run_due() == 1
    assert timer.state == "fired"


def test_named_signals_only_call_their_own_connected_slots(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="timeout", delay=1, now=100)
    timer.limit_reached.connect(InboxSlot("wrong-signal"))
    timer.timeout.connect(InboxSlot("right-signal"))
    timer.start()
    assert scheduler.run_due(now=101) == 1
    inbox = InboxStore(scheduler.redis)
    assert inbox.pending_items("wrong-signal") == []
    assert [item.text for item in inbox.pending_items("right-signal")] == ["timeout"]


def test_signal_disconnect_and_timer_cancel(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    connection = timer.timeout.connect(InboxSlot("disconnected"))
    timer.timeout.connect(InboxSlot("connected"))
    timer.timeout.disconnect(connection)
    timer.start()
    assert scheduler.run_due(now=101) == 1
    inbox = InboxStore(scheduler.redis)
    assert inbox.pending_items("disconnected") == []
    assert [item.text for item in inbox.pending_items("connected")] == ["notice"]
    cancelled = scheduler.timer(owner="operator", payload="cancelled", at=110, now=100)
    cancelled.timeout.connect(InboxSlot("cancelled"))
    cancelled.start()
    cancelled.cancel()
    assert cancelled.state == "cancelled"
    assert scheduler.run_due(now=120) == 0
    assert inbox.pending_items("cancelled") == []


def test_inbox_slot_delivers_to_a_seat_after_its_occupant_changes(scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.slots import InboxSlot

    inbox = InboxStore(scheduler.redis)
    seat = "eng-1@proof"
    inbox.seats.occupy(seat, "first-session", 1)
    timer = scheduler.timer(owner="operator", payload="seat notice", delay=5, now=100)
    timer.timeout.connect(InboxSlot(seat))
    timer.start()
    inbox.seats.occupy(seat, "next-session", 2)
    assert scheduler.run_due(now=105) == 1
    items = inbox.pending_items(seat)
    assert len(items) == 1
    assert items[0].address == seat
    assert items[0].text == "seat notice"
    assert inbox.seats.occupant(seat).occupant == "next-session"


def test_inbox_retry_after_transport_acceptance_keeps_one_item(monkeypatch, scheduler):
    from scripts.inbox.store import InboxStore
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import InboxSlot

    timer = scheduler.timer(owner="operator", payload="once", delay=1, now=100)
    timer.timeout.connect(InboxSlot("proof-session"))
    timer.start()
    original = InboxSlot.deliver

    def crash(self, event, delivery_id, redis):
        original(self, event, delivery_id, redis)
        raise RuntimeError("transport accepted before crash")

    with monkeypatch.context() as fault:
        fault.setattr(InboxSlot, "deliver", crash)
        with pytest.raises(RuntimeError, match="^transport accepted before crash$"):
            scheduler.run_due(now=101)
    inbox = InboxStore(scheduler.redis)
    first = inbox.pending_items("proof-session")
    assert len(first) == 1
    assert Scheduler(scheduler.redis, machine="proof").run_due(now=110) == 0
    assert inbox.pending_items("proof-session") == first
    assert timer.state == "fired"


def test_dispatch_lock_refuses_a_competing_drain_without_losing_work(scheduler):
    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=100)
    timer.start()
    lock = scheduler.redis.lock(scheduler.key("dispatch-lock"), timeout=30)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="^another scheduler is dispatching on this machine$"):
            scheduler.run_due(now=101)
    finally:
        lock.release()
    assert timer.state == "active"
    assert scheduler.run_due(now=101) == 1
    assert timer.state == "fired"


@pytest.mark.parametrize(
    "options,message",
    [
        ({}, "a timer needs either delay or at"),
        ({"delay": 1, "at": 100}, "a timer needs either delay or at"),
        ({"delay": -1}, "delay must not be negative"),
        ({"delay": 1, "owner": ""}, "a schedule needs an owner and payload"),
        ({"delay": 1, "payload": " "}, "a schedule needs an owner and payload"),
    ],
)
def test_timer_refuses_invalid_entries(scheduler, options, message):
    values = {"owner": "operator", "payload": "notice", "now": 100, **options}
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        scheduler.timer(**values)


def test_stopwatch_default_clock_and_negative_limit(monkeypatch, scheduler):
    from scripts.scheduler import core

    clock = [100]
    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: clock[0]))
    watch = scheduler.stopwatch(owner="operator", payload="limit", limit=10)
    watch.start()
    clock[0] = 104
    assert watch.elapsed() == 4
    watch.stop()
    clock[0] = 120
    assert watch.elapsed() == 4
    assert scheduler.run_due() == 0
    with pytest.raises(ValueError, match="^limit must not be negative$"):
        scheduler.stopwatch(owner="operator", payload="limit", limit=-1)


def test_cron_requires_five_fields_and_uses_the_default_clock(monkeypatch, scheduler):
    from scripts.scheduler import core

    monkeypatch.setattr(core, "time", SimpleNamespace(time=lambda: 0))
    assert scheduler.cron(owner="operator", payload="tick", expression="* * * * *").record["due"] == 60
    with pytest.raises(ValueError, match="^a cron expression needs five fields$"):
        scheduler.cron(owner="operator", payload="tick", expression="* * * * * *")
