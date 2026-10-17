import asyncio
import subprocess
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


async def test_one_runner_drains_work_and_refuses_a_second_runner(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    stop = asyncio.Event()
    calls = []

    def drain():
        calls.append("drained")
        stop.set()

    scheduler = SimpleNamespace(run_due=drain)
    with service.machine_lock("runner"):
        with pytest.raises(service.AlreadyRunning, match="^scheduler runner is already running$"):
            await service.run(scheduler, stop)
    await service.run(scheduler, stop)
    assert calls == ["drained"]
    with service.machine_lock("runner"):
        pass


def test_supervisor_restarts_an_exited_runner_without_machine_timers(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    children = [Mock(), Mock()]
    children[0].poll.return_value = 1
    children[1].poll.return_value = 2
    spawn = Mock(side_effect=children)
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL))
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    service.supervise(stop)
    assert spawn.call_count == 2
    assert [call.args[0] for call in spawn.call_args_list] == [
        [sys.executable, "-m", "scripts.scheduler.service", "run"],
        [sys.executable, "-m", "scripts.scheduler.service", "run"],
    ]
    assert all(call.kwargs == {"cwd": Path(service.__file__).resolve().parents[2]} for call in spawn.call_args_list)
    assert [call.args for call in stop.wait.call_args_list] == [(1,), (1,)]
    assert all(not child.terminate.called for child in children)


def test_supervisor_terminates_its_active_child_on_shutdown(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    child = Mock()
    child.poll.return_value = None
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=Mock(return_value=child)))
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.return_value = True
    service.supervise(stop)
    child.terminate.assert_called_once_with()
    child.wait.assert_called_once_with()


def test_agentihooks_starts_one_detached_supervisor_when_redis_is_configured(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    spawn = Mock()
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL))
    assert service.ensure_running({}) is False
    spawn.assert_not_called()
    environ = {"REDIS_URL": "redis://proof.invalid/0"}
    assert service.ensure_running(environ) is True
    call = spawn.call_args
    assert call.args[0] == [sys.executable, "-m", "scripts.scheduler.service", "supervise"]
    assert call.kwargs["env"] == environ
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["stdout"].name == str(tmp_path / "scheduler.log")
    assert call.kwargs["stderr"] is call.kwargs["stdout"]
    assert call.kwargs["stdout"].closed
    with service.machine_lock("supervisor"):
        assert service.ensure_running(environ) is False
        with pytest.raises(service.AlreadyRunning, match="^scheduler supervisor is already running$"):
            service.supervise()
    assert spawn.call_count == 1


def test_service_entrypoint_runs_the_configured_redis_scheduler(monkeypatch):
    from scripts.scheduler import service

    redis = object()
    received = []
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: redis)

    async def run(scheduler):
        received.append(scheduler.redis)

    monkeypatch.setattr(service, "run", run)
    assert service.main(["run"]) == 0
    assert received == [redis]


def test_service_entrypoint_reports_a_duplicate_runner(monkeypatch, capsys):
    from scripts.scheduler import service

    monkeypatch.setattr("scripts.swarm.store.redis_client", object)

    async def duplicate(scheduler):
        raise service.AlreadyRunning("scheduler runner is already running")

    monkeypatch.setattr(service, "run", duplicate)
    assert service.main(["run"]) == 2
    assert capsys.readouterr().err == "scheduler runner is already running\n"


def test_supervisor_entrypoint_stops_on_signals_and_restores_handlers(monkeypatch):
    from scripts.scheduler import service

    handlers = {15: object(), 2: object()}
    previous = dict(handlers)

    def wire(number, handler):
        handlers[number] = handler

    monkeypatch.setattr(service, "signal", SimpleNamespace(SIGTERM=15, SIGINT=2, getsignal=handlers.get, signal=wire))
    calls = []

    def supervise(stop):
        assert stop.is_set() is False
        handlers[15](15, None)
        assert stop.is_set() is True
        stop.clear()
        handlers[2](2, None)
        assert stop.is_set() is True
        calls.append("supervised")

    monkeypatch.setattr(service, "supervise", supervise)
    assert service.main(["supervise"]) == 0
    assert calls == ["supervised"]
    assert handlers == previous


def test_machine_lock_directory_is_shared_across_profile_configurations(monkeypatch):
    from scripts.scheduler import service

    directory = service.runtime_dir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(Path.home() / ".config" / "gh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "/proof/one")
    monkeypatch.setenv("AGENTIHOOKS_HOME", "/proof/two")
    assert service.runtime_dir() == directory
    assert directory.relative_to(Path.home()).parts == (".cache", "agentihooks", "scheduler")
