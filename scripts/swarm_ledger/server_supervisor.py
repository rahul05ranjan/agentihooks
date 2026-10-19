import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36
FORWARDED = (signal.SIGTERM, signal.SIGINT)


def subreap(libc=None) -> None:
    (libc or ctypes.CDLL(None)).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def forward(pidfile: Path, signum: int, kill=os.kill) -> None:
    try:
        kill(int(pidfile.read_text()), signum)
    except (OSError, ValueError):
        pass


def reap(wait=os.wait) -> int:
    status = 0
    while True:
        try:
            status = wait()[1]
        except ChildProcessError:
            return os.waitstatus_to_exitcode(status)


def supervise(
    argv: list[str], pidfile: Path, spawn=subprocess.Popen, libc=None, wait=os.wait, install=signal.signal
) -> int:
    subreap(libc)
    for signum in FORWARDED:
        install(signum, lambda received, frame: forward(pidfile, received))
    spawn(argv)
    return reap(wait)


def main(environ=os.environ) -> int:
    server = Path(__file__).resolve().parent / "ledger_server.py"
    return supervise([sys.executable, str(server), "--serve"], Path(environ["LEDGER_DIR"]) / ".server.pid")


if __name__ == "__main__":
    sys.exit(main())
