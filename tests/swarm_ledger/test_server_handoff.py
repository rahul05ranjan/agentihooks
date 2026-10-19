import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from scripts.swarm_ledger import server_handoff

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"
SUCCESSOR = (
    "import socket\n"
    "from scripts.swarm_ledger import server_handoff\n"
    "listener = server_handoff.inherited()\n"
    "server_handoff.announce(b'{\"demo\": 6}')\n"
    "client, _ = listener.accept()\n"
    "client.sendall(b'successor')\n"
    "client.close()\n"
)


@pytest.fixture
def listener():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        yield held


def child_env():
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def test_a_successor_that_reports_ready_serves_on_the_inherited_socket(listener, monkeypatch):
    monkeypatch.setattr(server_handoff.os, "environ", child_env())
    port = listener.getsockname()[1]
    assert server_handoff.hand_off(listener, [sys.executable, "-c", SUCCESSOR], timeout=30) == b'{"demo": 6}'
    listener.close()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        assert client.recv(64) == b"successor"


def test_a_successor_that_dies_before_ready_leaves_the_old_server_in_charge(listener, monkeypatch):
    monkeypatch.setattr(server_handoff.os, "environ", child_env())
    assert server_handoff.hand_off(listener, [sys.executable, "-c", "raise SystemExit(3)"], timeout=30) is None
    assert listener.fileno() >= 0


def test_a_successor_that_never_reports_is_killed_after_the_wait(listener, monkeypatch):
    monkeypatch.setattr(server_handoff.os, "environ", child_env())
    spawned = []
    popen = subprocess.Popen

    def recorded(*args, **kwargs):
        spawned.append(popen(*args, **kwargs))
        return spawned[-1]

    script = "import time\ntime.sleep(60)\n"
    assert server_handoff.hand_off(listener, [sys.executable, "-c", script], recorded, timeout=0.2) is None
    assert spawned[0].returncode == -signal.SIGKILL


def test_the_successor_gets_the_socket_and_the_ready_pipe_by_number(listener, monkeypatch):
    monkeypatch.setattr(server_handoff.os, "environ", {"KEEP": "1"})
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        os.write(int(kwargs["env"][server_handoff.READY_FD]), server_handoff.READY)
        return subprocess.Popen([sys.executable, "-c", ""])

    assert server_handoff.hand_off(listener, ["serve"], popen, timeout=5) == b""
    env = seen["env"]
    assert seen["argv"] == ["serve"]
    assert env["KEEP"] == "1"
    assert env[server_handoff.LISTEN_FD] == str(listener.fileno())
    assert seen["pass_fds"] == (listener.fileno(), int(env[server_handoff.READY_FD]))
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["start_new_session"] is True


def test_a_failed_spawn_closes_both_pipe_ends(listener, monkeypatch):
    monkeypatch.setattr(server_handoff.os, "environ", {})
    opened = []
    pipe = os.pipe

    def recorded():
        opened.extend(pipe())
        return tuple(opened)

    monkeypatch.setattr(server_handoff.os, "pipe", recorded)

    def popen(argv, **kwargs):
        raise OSError("no such interpreter")

    with pytest.raises(OSError, match="no such interpreter"):
        server_handoff.hand_off(listener, ["serve"], popen)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_inherited_takes_the_socket_and_forgets_it():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        environ = {server_handoff.LISTEN_FD: str(os.dup(held.fileno()))}
        taken = server_handoff.inherited(environ)
        try:
            assert taken.getsockname() == held.getsockname()
            assert environ == {}
        finally:
            taken.close()


def test_no_inherited_socket_without_the_setting():
    assert server_handoff.inherited({}) is None


def test_announce_writes_ready_with_its_payload_once_and_forgets_the_pipe():
    read, write = os.pipe()
    environ = {server_handoff.READY_FD: str(write)}
    server_handoff.announce(b"held", environ)
    assert os.read(read, 64) == server_handoff.READY + b"held"
    assert os.read(read, 8) == b""
    assert environ == {}
    os.close(read)
    server_handoff.announce(b"held", environ)


def healthy(port, timeout=5):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
        return resp.status


def pid_of(folder):
    try:
        return int((folder / ".server.pid").read_text())
    except (OSError, ValueError):
        return None


def until(check, limit=30):
    deadline = time.monotonic() + limit
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.wall_clock
def test_a_reload_hands_over_without_a_failed_or_slow_request(tmp_path, ledger_port):
    env = {
        **child_env(),
        "LEDGER_DIR": str(tmp_path),
        "LEDGER_PORT": str(ledger_port),
        "SWARM_RELOAD": "0",
    }
    env.pop("LEDGER_RUN_PID", None)
    env.pop("LEDGER_RUN_START", None)
    with (tmp_path / "server.out").open("w") as log:
        first = subprocess.Popen([sys.executable, str(SERVER), "--serve"], env=env, stdout=log, stderr=log)
    successor = None
    try:
        until(lambda: pid_of(tmp_path) == first.pid)
        results, stop = [], threading.Event()

        def hammer():
            while not stop.is_set():
                started = time.monotonic()
                try:
                    results.append((healthy(ledger_port), time.monotonic() - started))
                except OSError as exc:
                    results.append((repr(exc), time.monotonic() - started))
                time.sleep(0.005)

        worker = threading.Thread(target=hammer, daemon=True)
        worker.start()
        time.sleep(0.2)
        os.kill(first.pid, signal.SIGHUP)
        first.wait(timeout=30)
        successor = pid_of(tmp_path)
        time.sleep(0.2)
        stop.set()
        worker.join(10)
        assert successor not in (None, first.pid)
        assert healthy(ledger_port) == 200
        assert results and {status for status, _ in results} == {200}, (tmp_path / "server.out").read_text()
        assert max(seconds for _, seconds in results) < 1.0
    finally:
        for pid in {first.pid, successor} - {None}:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        first.wait(timeout=10)
