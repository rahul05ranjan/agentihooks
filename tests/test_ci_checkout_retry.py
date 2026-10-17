import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _checkout_steps(job):
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    return [step for step in workflow["jobs"][job]["steps"] if step.get("uses", "").startswith("actions/checkout@")]


@pytest.mark.parametrize("job", ["unit", "unit-selected"])
def test_partial_checkout_has_two_bounded_attempts_and_a_strict_final_result(job):
    first, second = _checkout_steps(job)
    assert first["continue-on-error"] is True
    assert second.get("continue-on-error", False) is False
    assert second["if"] == "${{ !cancelled() && steps.checkout.outcome == 'failure' }}"
    assert first["id"] == "checkout"
    assert (
        first["with"]
        == second["with"]
        == {
            "ref": "${{ github.sha }}",
            "fetch-depth": 0,
            "filter": "blob:none",
        }
    )
    for step in (first, second):
        assert 0 < step["timeout-minutes"] <= 2
        assert step["env"] == {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.lowSpeedLimit",
            "GIT_CONFIG_VALUE_0": "1024",
            "GIT_CONFIG_KEY_1": "http.lowSpeedTime",
            "GIT_CONFIG_VALUE_1": "30",
        }


def _git(directory, *args, env=None, check=True):
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=10,
    )


@pytest.mark.parametrize("stalls", [0, 1, 2])
def test_promised_blob_checkout_recovers_within_bound_without_old_blobs(tmp_path, stalls):
    attempts = _checkout_steps("unit")
    assert len(attempts) == 2
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.name", "Checkout proof")
    _git(source, "config", "user.email", "checkout@example.invalid")
    tracked = source / "tracked"
    tracked.write_text("historical contents\n")
    _git(source, "add", "tracked")
    _git(source, "commit", "-m", "Historical tree")
    old_blob = _git(source, "rev-parse", "HEAD:tracked").stdout.strip()
    tracked.write_text("current contents\n")
    _git(source, "commit", "-am", "Current tree")
    target = _git(source, "rev-parse", "HEAD").stdout.strip()
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(bare))
    _git(bare, "config", "uploadpack.allowFilter", "true")
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--filter=blob:none", "--no-checkout", bare.as_uri(), str(checkout))
    stop = threading.Event()
    requests = []

    class Transport(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            requests.append(self.path)
            if len(requests) <= stalls:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-git-upload-pack-result")
                self.end_headers()
                self.wfile.flush()
                stop.wait(8)
                return
            self.respond()

        def respond(self):
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            result = subprocess.run(
                ["git", "http-backend"],
                input=self.rfile.read(length),
                capture_output=True,
                check=True,
                timeout=5,
                env={
                    **os.environ,
                    "GIT_PROJECT_ROOT": str(tmp_path),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": url.path,
                    "QUERY_STRING": url.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                },
            )
            headers, body = result.stdout.split(b"\r\n\r\n", 1)
            self.send_response(200)
            for header in headers.decode().split("\r\n"):
                name, value = header.split(": ", 1)
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Transport)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    _git(checkout, "remote", "set-url", "origin", f"http://127.0.0.1:{server.server_port}/remote.git")
    started = time.monotonic()
    results = []
    try:
        for attempt in attempts:
            env = {**os.environ, **attempt["env"], "GIT_CONFIG_VALUE_1": "1"}
            result = _git(checkout, "checkout", "--detach", target, env=env, check=False)
            results.append(result)
            if result.returncode == 0:
                break
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert time.monotonic() - started < 8
    assert len(results) == min(stalls + 1, 2)
    assert len(requests) == len(results)
    for result in results[:stalls]:
        assert result.returncode != 0
        assert "Operation too slow" in result.stderr
    if stalls < 2:
        assert results[-1].returncode == 0, results[-1].stderr
        assert (checkout / "tracked").read_text() == "current contents\n"
    else:
        assert results[-1].returncode != 0
        assert not (checkout / "tracked").exists()
    assert _git(checkout, "rev-list", "--count", "HEAD").stdout.strip() == "2"
    missing = _git(checkout, "rev-list", "--objects", "--all", "--missing=print").stdout
    assert f"?{old_blob}" in missing
