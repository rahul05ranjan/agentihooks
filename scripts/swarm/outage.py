import fcntl
import json
from pathlib import Path

from scripts.swarm_v2.records import _write

HOME = Path.home() / ".agentihooks" / "swarm"


def read(slug: str) -> dict:
    path = HOME / slug / "outages.json"
    return json.loads(path.read_text()) if path.exists() else {}


def observe(slug: str, kind: str, evidence: list[str], now_ms: int) -> None:
    folder = HOME / slug
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "outages.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read(slug)
        record = records.get(kind)
        if evidence:
            started = record["started_at"] if record and record["ended_at"] is None else now_ms
            records[kind] = {"kind": kind, "started_at": started, "evidence": evidence, "ended_at": None}
        elif record and record["ended_at"] is None:
            record["ended_at"] = now_ms
        else:
            return
        _write(folder / "outages.json", records)


def open_records(slug: str) -> list[dict]:
    return [record for record in read(slug).values() if record["ended_at"] is None]
