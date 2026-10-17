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
