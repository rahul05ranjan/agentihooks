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


def test_runtime_idle_is_distinct_from_productive_waiting(rig):
    idle.declare_wait(rig.store.redis, SLUG, rig.agent.name, 2000, "checks", 1000)
    assert rig.runner.status(rig.agent) == "idle"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.WAITING
    rig.thread["turns"] = [{"id": "turn", "status": "inProgress"}]
    assert rig.runner.status(rig.agent) == "working"
    assert idle.of(rig.store, rig.runner, SLUG, rig.agent, 1000) == idle.WORKING


def test_server_loss_is_visible_even_when_the_viewer_survives(rig, monkeypatch):
    from scripts import terminate_agent

    monkeypatch.setattr(terminate_agent, "sessions", lambda: [SimpleNamespace(name=rig.agent.name, status="alive")])
    monkeypatch.setattr(supervisor, "start_time", lambda pid: 999)
    assert rig.agent.name not in rig.runner.live_names()
    assert not rig.runner.reported(rig.agent)
    assert rig.runner.status(rig.agent) == "dead"
    assert rig.runner.bindings([rig.agent]) == {rig.agent.name: {"process": False}}


def test_an_unavailable_thread_is_unknown_and_keeps_the_live_execution(rig):
    rig.connection.request.side_effect = protocol.ProtocolError("connection lost")
    assert rig.runner.status(rig.agent) == "unknown"
    assert rig.agent.name in rig.runner.live_names()
    rig.connection.close.assert_called_once()


def test_missing_server_identity_holds_the_execution_without_using_its_viewer(rig):
    rig.bindings.put(SLUG, replace(rig.record, server_start_identity=0))
    assert rig.runner.status(rig.agent) == "unknown"
    assert rig.agent.name in rig.runner.live_names()
    assert rig.runner.bindings([rig.agent]) == {rig.agent.name: {"process": None}}
    rig.herdr.assert_not_called()


def test_managed_conversation_discovery_uses_the_recorded_thread(rig):
    assert rig.runner.conversations() == {"viewer": "thread"}


def test_managed_retirement_uses_the_server_start_identity(rig, tmp_path):
    from scripts.swarm import reaper

    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire(rig.agent, homes=(tmp_path,))
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)
    assert rig.bindings.get(SLUG, rig.agent.execution_id).state == "exited"
    assert not rig.store.redis.hgetall(rig.store.key(SLUG, "codex-endpoints"))
    rig.herdr.assert_not_called()


def test_managed_retire_process_uses_the_server_instead_of_the_launcher(rig):
    from scripts.swarm import reaper

    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    assert rig.runner.retire_process(rig.agent, 99, (), 100)
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)


def test_managed_retirement_refuses_an_unknown_start_identity(rig):
    rig.bindings.put(SLUG, replace(rig.record, server_start_identity=0))
    rig.runner.end = Mock()
    assert not rig.runner.retire(rig.agent)
    rig.runner.end.assert_not_called()
    assert rig.runner.refusal(rig.agent)["refusal"] == "managed server identity is unknown"


def test_local_managed_retirement_does_not_need_a_live_launcher(rig):
    from scripts.swarm import reaper
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    rig.runner.end = Mock(return_value=reaper.Outcome((123,)))
    local = LocalHerdrRuntime(rig.runner, table=Mock(side_effect=AssertionError("launcher is unavailable")))
    assert local.terminate(rig.agent).ok
    rig.runner.end.assert_called_once_with(rig.agent.name, 123, [], 456)


def test_an_unrelated_named_session_survives_managed_retirement(rig, monkeypatch):
    import os
    import subprocess
    import sys

    from hooks.observability.trace_flush import start_time

    monkeypatch.setattr(supervisor, "start_time", start_time)
    argv = [sys.executable, "-c", "import time; time.sleep(300)"]
    env = {**os.environ, "AGENTIHOOKS_AGENT_NAME": rig.agent.name}
    with subprocess.Popen(argv, env=env, start_new_session=True) as server:
        with subprocess.Popen(argv, env=env, start_new_session=True) as unrelated:
            try:
                record = replace(rig.record, server_pid=server.pid, server_start_identity=start_time(server.pid))
                rig.bindings.put(SLUG, record)
                assert rig.runner.retire(rig.agent)
                server.wait(timeout=5)
                assert unrelated.poll() is None
            finally:
                server.terminate()
                unrelated.terminate()
