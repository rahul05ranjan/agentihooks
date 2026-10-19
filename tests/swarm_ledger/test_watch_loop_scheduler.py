import threading

import pytest

from scripts.scheduler import process_timer
from scripts.swarm_ledger import ledger_server

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


class Stop(Exception):
    pass


@pytest.fixture
def unwritable_cache(monkeypatch, tmp_path):
    import fakeredis

    (tmp_path / ".cache").write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    for name in ("AGENTIHOOKS_DEPLOYMENT", "AGENTIHOOKS_HIVE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(process_timer, "blocking_client", lambda: fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event(), raising=False)


def test_the_watch_loop_reaches_its_second_pass_when_the_scheduler_lock_folder_cannot_be_created(
    unwritable_cache, monkeypatch, capsys
):
    passes, slept = [], []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise Stop

    for name in ("bin_closed_without_swarm", "expire_alerts"):
        monkeypatch.setattr(ledger_server, name, lambda: None)
    monkeypatch.setattr(ledger_server, "sample_streams", lambda: passes.append(1))
    monkeypatch.setattr(ledger_server.ledger_bin, "tidy", lambda: None)
    monkeypatch.setattr(ledger_server, "reloading", lambda *args: False)
    monkeypatch.setattr(ledger_server, "unrelayed", lambda: {})
    monkeypatch.setattr(ledger_server, "metrics_journal", lambda *args: None)
    monkeypatch.setattr(ledger_server.time, "sleep", sleep)
    with pytest.raises(Stop):
        ledger_server.watch_ledgers()
    assert passes == [1, 1, 1]
    assert slept == [2.0, 2.0, 2.0]
    assert capsys.readouterr().err.count("scheduler wait unavailable, sleeping instead") == 1


def test_a_working_scheduler_wait_is_the_only_wait(monkeypatch):
    waited = []
    monkeypatch.setattr(ledger_server, "SCHEDULER_UNAVAILABLE", threading.Event(), raising=False)
    monkeypatch.setattr(process_timer, "wait", waited.append)
    monkeypatch.setattr(ledger_server.time, "sleep", lambda seconds: pytest.fail(f"slept {seconds}"))
    ledger_server.scheduled_wait(2.0)
    assert waited == [2.0]
    assert not ledger_server.SCHEDULER_UNAVAILABLE.is_set()
