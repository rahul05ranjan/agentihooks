import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


def run_codex(home: Path, timeout: float = 20) -> dict:
    registration = home / ".agentihooks" / "active-sessions.json"
    started = time.monotonic()
    with (home / "codex-run.txt").open("w") as output:
        process = subprocess.Popen(
            ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust", "hello"],
            env=os.environ | {"HOME": str(home)},
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while time.monotonic() - started < timeout:
                try:
                    entries = json.loads(registration.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    entries = {}
                if [entry["cwd"] for entry in entries.values()] == [str(Path.cwd())]:
                    registered_at = registration.stat().st_mtime
                    break
                if process.poll() is not None:
                    sys.exit("Codex session registration missing")
                time.sleep(0.1)
            else:
                sys.exit("Codex session registration missing")
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    latency = time.time() - registered_at
    assert latency < 2, f"Codex cleanup exceeded two seconds: {latency}"
    return {"registration_to_exit_seconds": latency, "elapsed_seconds": time.monotonic() - started}


if __name__ == "__main__":
    print(json.dumps(run_codex(Path(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 20)))
