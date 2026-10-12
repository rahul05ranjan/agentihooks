from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.codex_session import protocol, supervisor
from scripts.codex_session.binding import CONNECTED, BindingStore, TransportRecord
from scripts.swarm import idle, live_binding, runtime
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group(name="fakeredis")]
SLUG = "managed"


@pytest.fixture
def rig(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
    agent = store.start_execution(
        SLUG,
        AgentRecord(
            store.next_name(SLUG, "eng"),
            "eng",
            "task",
            pane_id="viewer",
            harness="codex",
            seat=f"eng-1@{SLUG}",
            transport=APP_SERVER_TRANSPORT,
            profile_decision={"validation": {"pid": 99}},
        ),
    )
    bindings = BindingStore(store)
    record = bindings.reserve(
        SLUG,
        TransportRecord(
            agent.execution_id,
            agent.generation,
            agent.seat,
            agent.name,
            agent.task,
            "unix:///run/managed/server.sock",
            server_pid=123,
            server_start_identity=456,
            thread_id="thread",
        ),
    )
    bindings.put(SLUG, replace(record, state=CONNECTED))
    thread = {"id": "thread", "turns": []}
    connection = Mock()
    connection.request.side_effect = lambda method, params: {"thread": thread}
    monkeypatch.setattr(protocol, "connect", lambda path, timeout: (connection, {}))
    monkeypatch.setattr(supervisor, "start_time", lambda pid: 456 if pid == 123 else None)
    monkeypatch.setattr(runtime, "connect", lambda: store)
    monkeypatch.setattr(live_binding, "read", Mock(return_value={"process": True}))
    from scripts import terminate_agent

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    herdr = Mock(side_effect=AssertionError("managed execution must not inspect a pane"))
    runner = runtime.HerdrRuntime(herdr=herdr)
    return SimpleNamespace(
        store=store,
        agent=agent,
        bindings=bindings,
        record=record,
        thread=thread,
        connection=connection,
        runner=runner,
        herdr=herdr,
    )


def test_managed_server_and_thread_are_live_without_a_viewer(rig):
    assert rig.agent.name in rig.runner.live_names()
    assert rig.runner.reported(rig.agent)
    assert rig.runner.bindings([rig.agent]) == {rig.agent.name: {"process": True}}
    live_binding.read.assert_called_once_with(rig.agent, 123)
    assert rig.runner.status(rig.agent) == "idle"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.IDLE
    rig.connection.request.assert_called_with("thread/read", {"threadId": "thread", "includeTurns": True})
    rig.herdr.assert_not_called()
