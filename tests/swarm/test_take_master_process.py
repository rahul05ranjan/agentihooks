from types import SimpleNamespace

import pytest

from hooks.proc import Process
from scripts.swarm import take_master
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm_v2.runtime import process
from scripts.swarm_v2.runtime.base import RuntimeRouter
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.swarm.test_cli import env  # noqa: F401
from tests.swarm.test_take_master import taker  # noqa: F401
from tests.swarm.test_take_master_remote import remote_taker  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def native_taker(remote_taker, monkeypatch):  # noqa: F811
    store = remote_taker[0]
    table = {
        pid: Process(pid, 1, pid, pid, start, "S", "codex", ("codex",))
        for pid, start in ((4242, 100), (4343, 200))
    }
    monkeypatch.setattr(process, "local_namespace", lambda: "boot/pidns")
    monkeypatch.setattr(take_master, "processes", lambda: table, raising=False)
    monkeypatch.setattr(take_master, "name_session", lambda pid, name: True)
    monkeypatch.setattr(take_master, "launch_of", lambda pid: ("account", {"pid": pid, "profile": "master"}))
    herdr = HerdrRuntime()
    retired = []

    def end(name, pid, homes, start):
        retired.append((pid, start))
        return SimpleNamespace(process=pid or 0, refusal="")

    herdr.end = end
    herdr.live_names = lambda: set()
    local = LocalHerdrRuntime(herdr, namespace=lambda: "boot/pidns", table=lambda: table)
    runtime = RoutedRuntime(herdr, RuntimeRouter([local]))
    return store, runtime, table, retired


def test_a_gone_native_master_is_retired_and_replaced(native_taker, monkeypatch):
    store, runtime, table, retired = native_taker
    old, _ = take_master.take(store, "sw", "old-session", runtime, 1000)
    table.pop(4242)
    monkeypatch.setattr(take_master, "agent_pid", lambda: 4343)

    new, _ = take_master.take(store, "sw", "new-session", runtime, 2000, replace_live=True)

    assert retired == [(None, 100)]
    assert store.agent("sw", old.name) is None
    assert store.execution_occupants("sw")[new.seat].execution_id == new.execution_id
    assert new.runtime_target == {
        "host_id": "member-1",
        "process_namespace": "boot/pidns",
        "pid": 4343,
        "pid_start": 200,
    }
