import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"
CODE = ROOT / "scripts" / "swarm_ledger"
IN_FLIGHT = 20


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start(folder, port, reload):
    env = {**os.environ, "LEDGER_DIR": str(folder), "LEDGER_HOST": "127.0.0.1", "LEDGER_PORT": str(port)}
    env.update({"SWARM_RELOAD": reload, "PYTHONPATH": str(ROOT)})
    log = (folder / "probe.log").open("a")
    return subprocess.Popen([sys.executable, str(SCRIPT), "--serve"], env=env, stdout=log, stderr=log)


def head(port):
    return f"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n".encode()


def answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5) as conn:
            conn.settimeout(2)
            conn.sendall(head(port) + b"\r\n")
            return conn.recv(32).startswith(b"HTTP/1.0 200")
    except OSError:
        return False


def wait_up(port, limit=30):
    started = time.monotonic()
    while not answers(port):
        assert time.monotonic() - started < limit, "server never answered"
        time.sleep(0.05)
    return time.monotonic() - started


def starts(folder):
    return (folder / "probe.log").read_text().count("ledger server on")


def open_partial(port):
    held = []
    for _ in range(IN_FLIGHT):
        conn = socket.create_connection(("127.0.0.1", port), 2)
        conn.sendall(head(port))
        held.append(conn)
    time.sleep(0.5)
    return held


def finish(held):
    answered = reset = 0
    for conn in held:
        try:
            conn.sendall(b"\r\n")
            conn.settimeout(10)
            ok = conn.recv(32).startswith(b"HTTP/1.0 200")
        except OSError:
            ok = False
        finally:
            conn.close()
        answered += ok
        reset += not ok
    return answered, reset


def gap(port, limit=30):
    started = time.monotonic()
    first_down = None
    while time.monotonic() - started < limit:
        up = answers(port)
        if not up and first_down is None:
            first_down = time.monotonic()
        if up and first_down is not None:
            return round(time.monotonic() - first_down, 2)
        time.sleep(0.02)
    return None


def rewrite_same():
    for path in CODE.glob("*.py"):
        path.write_bytes(path.read_bytes())


def change_content():
    target = CODE / "ledger_title.py"
    target.write_text(target.read_text() + "\n")


def test_probe(tmp_path):
    report = {"sha": os.environ.get("GITHUB_SHA", "")}
    port = free_port()
    child = start(tmp_path, port, "1")
    try:
        report["first_start_s"] = round(wait_up(port), 2)
        time.sleep(2.5)
        rewrite_same()
        time.sleep(6)
        report["reloads_after_same_content_rewrite"] = starts(tmp_path) - 1
        wait_up(port)
        before = starts(tmp_path)
        change_content()
        report["restart_gap_s_after_content_change"] = gap(port)
        wait_up(port)
        time.sleep(3)
        report["reloads_after_content_change"] = starts(tmp_path) - before
        held = open_partial(port)
        change_content()
        time.sleep(4)
        report["reload_in_flight"], report["reload_reset"] = finish(held)
        wait_up(port)
        held = open_partial(port)
        child.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        report["stop_in_flight"], report["stop_reset"] = finish(held)
        child.wait(15)
        report["stop_exit"] = child.returncode
    finally:
        if child.poll() is None:
            child.kill()
    raise AssertionError("PROBE " + json.dumps(report))
