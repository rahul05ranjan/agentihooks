"""`python -m scripts.codex_session attach`: open the native Codex terminal on an execution's recorded thread.

The terminal is a viewer of the supervised app-server; closing it leaves the server, the thread and inbox delivery
running, and attaching again resumes the same thread.
"""

import argparse
import subprocess
import sys
import time
from collections.abc import Callable

from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, TransportRecord

READY_TIMEOUT_S = 120.0
POLL_S = 0.5
ATTEMPTS = 3
EARLY_EXIT_S = 5.0
ENDED = "the execution's app-server {}; there is no thread to attach"
NOT_READY = "the execution has no connected thread after {:g} seconds"


class AttachError(RuntimeError):
    pass


def viewer_argv(codex_bin: str, record: TransportRecord) -> list[str]:
    return [codex_bin, "resume", "--remote", record.endpoint_ref, record.thread_id]


def attachable(record: TransportRecord | None) -> bool:
    return record is not None and record.state == CONNECTED and bool(record.thread_id)


def wait_attachable(
    read: Callable[[], TransportRecord | None],
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TransportRecord:
    deadline = clock() + timeout
    while True:
        record = read()
        if attachable(record):
            return record
        if record is not None and record.state in (FAILED, EXITED):
            raise AttachError(ENDED.format(record.state))
        if clock() >= deadline:
            raise AttachError(NOT_READY.format(timeout))
        sleep(POLL_S)


def attach(
    read: Callable[[], TransportRecord | None],
    run: Callable[[list[str]], int],
    codex_bin: str,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """A viewer that exits at once is retried: the thread accepts resume only after its first turn wrote a rollout."""
    code = 0
    for _ in range(ATTEMPTS):
        record = wait_attachable(read, timeout, clock, sleep)
        started = clock()
        code = run(viewer_argv(codex_bin, record))
        if code == 0 or clock() - started >= EARLY_EXIT_S:
            return code
        sleep(POLL_S)
    return code


def recorded(slug: str, name: str) -> Callable[[], TransportRecord | None]:
    from scripts.swarm import live_binding
    from scripts.swarm.store import connect

    store = connect()

    def read() -> TransportRecord | None:
        agent = next((agent for agent in store.agents(slug) if agent.name == name), None)
        return None if agent is None else live_binding.transport(store, agent)

    return read


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.codex_session attach")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=float, default=READY_TIMEOUT_S)
    return parser


def main(argv: list[str], read: Callable | None = None, run: Callable | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return attach(
            read or recorded(args.slug, args.agent),
            run or (lambda command: subprocess.run(command).returncode),
            args.codex_bin,
            args.timeout,
        )
    except AttachError as exc:
        print(f"agentihooks codex-session: {exc}", file=sys.stderr)
        return 3
