"""Local outbox for inbox writes made while Redis is unreachable.

Append only JSON lines under the agentihooks home, one checksummed record per send, reply or close. Each record names
the item it writes, assigned when the record is written, so a replay never writes twice. Every entry point that
connects to the inbox flushes the outbox first, in order; a corrupt record or one Redis refuses is reported and set
aside in the skipped file, never replayed again. A full outbox refuses the new write and keeps every queued record.
"""

import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from scripts.inbox.store import DuplicateItem, InboxError, new_item_id, now_ms

NAME = "inbox-outbox.jsonl"
SKIPPED = "inbox-outbox.skipped.jsonl"
LOCK = "inbox-outbox.lock"
OPS = ("send", "reply", "close")
MAX_ENV = "AGENTIHOOKS_INBOX_OUTBOX_MAX_BYTES"
WARN_ENV = "AGENTIHOOKS_INBOX_OUTBOX_WARN_BYTES"
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_WARN_BYTES = 786_432
QUEUED = "agentihooks msg: Redis is unreachable; the message is queued locally and replays when Redis returns"


class OutboxFull(InboxError):
    pass


@dataclass
class Report:
    applied: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    corrupt: int = 0
    left: int = 0


def home() -> Path:
    from hooks import config

    return Path(config.AGENTIHOOKS_HOME)


def path() -> Path:
    return home() / NAME


def skipped_path() -> Path:
    return home() / SKIPPED


def bounds(environ=None) -> tuple[int, int]:
    env = os.environ if environ is None else environ
    return int(env.get(MAX_ENV) or DEFAULT_MAX_BYTES), int(env.get(WARN_ENV) or DEFAULT_WARN_BYTES)


def checksum(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record(op: str, by: str, args: dict, item: str = "") -> dict:
    body = {"op": op, "by": by, "item": item or new_item_id(), "args": args, "at": now_ms()}
    return {**body, "sum": checksum(body)}


def parse(line: str) -> dict | None:
    try:
        found = json.loads(line)
    except ValueError:
        return None
    if not isinstance(found, dict) or found.get("op") not in OPS:
        return None
    body = {key: value for key, value in found.items() if key != "sum"}
    return found if found.get("sum") == checksum(body) else None


def waiting() -> bool:
    return path().exists() and path().stat().st_size > 0


@contextmanager
def locked(blocking=True):
    home().mkdir(parents=True, exist_ok=True)
    with (home() / LOCK).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append(entry: dict, environ=None) -> None:
    line = json.dumps(entry, sort_keys=True) + "\n"
    limit, warn = bounds(environ)
    with locked():
        size = path().stat().st_size if path().exists() else 0
        grown = size + len(line.encode())
        if grown > limit:
            raise OutboxFull(
                f"the local inbox outbox is full ({size} of {limit} bytes, {MAX_ENV}); this message was not "
                "queued and nothing queued was dropped: bring Redis back so the outbox replays"
            )
        with path().open("a") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    if grown >= warn:
        print(
            f"agentihooks msg: warning: the local inbox outbox holds {grown} bytes of its {limit} byte limit",
            file=sys.stderr,
        )


def submit(store, entry: dict, apply) -> dict:
    """Apply entry now when Redis answers and nothing waits ahead of it, else queue it in the outbox."""
    from redis.exceptions import RedisError

    if store is not None and not waiting():
        try:
            return apply(store, entry)
        except RedisError:
            pass
    append(entry)
    print(QUEUED, file=sys.stderr)
    return {"id": entry["item"], "op": entry["op"], "state": "queued"}


def flush(store) -> Report:
    """Replay every queued record in order; stop at the first Redis error and keep it and the rest for later."""
    report = Report()
    if not waiting():
        return report
    with locked(blocking=False) as held:
        if held:
            _replay(store, report)
    _tell(report)
    return report


def _replay(store, report: Report) -> None:
    from redis.exceptions import RedisError

    from scripts.inbox.cli import apply

    lines = path().read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        entry = parse(line)
        if entry is None:
            _set_aside(line, "corrupt")
            report.corrupt += 1
            continue
        try:
            apply(store, entry)
            report.applied.append(entry["item"])
        except DuplicateItem:
            report.already.append(entry["item"])
        except RedisError:
            _rewrite(lines[index:])
            report.left = len(lines) - index
            return
        except InboxError as exc:
            _set_aside(line, str(exc))
            report.rejected.append((entry["item"], str(exc)))
    _rewrite([])


def _set_aside(line: str, reason: str) -> None:
    with skipped_path().open("a") as handle:
        handle.write(json.dumps({"reason": reason, "line": line.rstrip("\n")}) + "\n")


def _rewrite(lines: list[str]) -> None:
    staged = path().with_suffix(".tmp")
    staged.write_text("".join(lines))
    os.replace(staged, path())


def _tell(report: Report) -> None:
    for _ in range(report.corrupt):
        print(f"agentihooks msg: a corrupt outbox record was skipped and kept in {skipped_path()}", file=sys.stderr)
    for item, reason in report.rejected:
        print(
            f"agentihooks msg: queued message {item} was refused on replay ({reason}); kept in {skipped_path()}",
            file=sys.stderr,
        )
