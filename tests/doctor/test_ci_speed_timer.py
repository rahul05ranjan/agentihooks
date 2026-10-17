import json

import fakeredis
import pytest

from scripts.doctor import ci_speed_read, cli, loop
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.doctor.test_ci_speed import NOW, job, record

pytestmark = pytest.mark.unit
DOCTOR = "speed-doctor"
WATCHED = "speed-watched"


@pytest.fixture
def store(monkeypatch):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(DOCTOR, "/repo", 1, 0, state="drained", template="doctor"))
    store.set_peer(DOCTOR, WATCHED)
    monkeypatch.setattr(cli.detect, "readers", lambda *args, **kwargs: {})
    return store


def snapshot(records=(), **extra):
    return {"complete": True, "runs": list(records), "api_calls": 7, "discovered_runs": len(records), **extra}


def test_every_timer_tick_reads_speed_and_sends_each_crossing_to_both_masters_once(store, monkeypatch):
    calls = []

    def read(since, now, pending=None):
        calls.append((since, now, pending))
        return snapshot([record([job(0, queued=True, created=-301)])])

    monkeypatch.setattr(ci_speed_read, "read", read)
    first = cli.timer(store, DOCTOR, NOW, False)
    second = cli.timer(store, DOCTOR, NOW + 60000, False)
    assert calls == [(NOW - 60000, NOW, None), (NOW, NOW + 60000, [7])]
    for slug in [DOCTOR, WATCHED]:
        [item] = InboxStore(store.redis).inbox(f"master@{slug}")
        assert "run 7" in item.text and "anton-oss" in item.text and "301 seconds" in item.text
    assert any("1 runs" in action and "1 jobs" in action and "7 API calls" in action for action in first)
    assert any("CI speed" in action for action in second)
    finding, _ = loop.judged(store, DOCTOR, "ci-runner-wait/7-1-11")
    assert finding["measure"] == 361


def test_an_api_failure_reports_incomplete_counts_and_preserves_the_window_for_retry(store, monkeypatch):
    calls = []
    outcomes = [snapshot(), snapshot(complete=False, error="API unavailable"), snapshot()]

    def read(since, now, pending=None):
        calls.append((since, pending))
        return outcomes.pop(0)

    monkeypatch.setattr(ci_speed_read, "read", read)
    cli.timer(store, DOCTOR, NOW, False)
    actions = cli.timer(store, DOCTOR, NOW + 60000, False)
    cli.timer(store, DOCTOR, NOW + 120000, False)
    assert calls == [(NOW - 60000, None), (NOW, []), (NOW, [])]
    assert any("incomplete" in action and "API unavailable" in action for action in actions)
    [item] = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert "API unavailable" in item.text


def test_stopped_or_unlinked_doctors_do_not_read_the_api(store, monkeypatch):
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: pytest.fail("unexpected API read"))
    store.update(DOCTOR, state="stopped")
    assert cli.timer(store, DOCTOR, NOW, False) == []
    store.update(DOCTOR, state="drained")
    store.clear_peer(DOCTOR)
    assert cli.timer(store, DOCTOR, NOW, False) == []


def test_a_complete_pass_records_full_counts_and_the_next_active_set(store, monkeypatch):
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([], status="completed")]))
    cli.timer(store, DOCTOR, NOW, False)
    saved = json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))
    assert saved["last"] == NOW and saved["pending"] == []
    assert saved["report"]["runs"] == 1 and saved["report"]["jobs"] == 0
