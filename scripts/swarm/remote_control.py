"""Remote Control for interactive Claude masters: launch flags, the status line, and recorded failure notices."""

import json
import os
import time
from collections.abc import Mapping

from scripts.inbox.seats import seat_address
from scripts.swarm.store import MASTER, RedisStore, connect

FLAG = "AGENTIHOOKS_MASTER_REMOTE_CONTROL"
FAILURES = "remote-control-failures"
FALLBACK = "fallback auth"
GATEWAY = "gateway"
OFF = "flag"
FAILURE_WORDS = ("Remote Control failed", "Remote Control disconnected")


def now_ms() -> int:
    return int(time.time() * 1000)


def session_name(seat: str, slug: str) -> str:
    return f"{seat.partition('@')[0]} · {slug}"


def name_prefix() -> str:
    from scripts.swarm.commands import hive_id

    return hive_id()


def off_reason(route_kind: str, environ: Mapping[str, str]) -> str:
    if environ.get(FLAG, "").lower() in ("0", "false", "off"):
        return OFF
    if route_kind == "api":
        return GATEWAY
    return "" if route_kind == "interactive" else FALLBACK


def decision(lane: str, harness: str, route_kind: str, slug: str, environ: Mapping[str, str]) -> dict:
    if lane != MASTER or harness != "claude":
        return {}
    reason = off_reason(route_kind, environ)
    if reason:
        return {"state": "off", "detail": reason}
    return {"state": "on", "detail": session_name(seat_address(slug, MASTER), slug)}


def arguments(decided: Mapping) -> list[str]:
    if decided.get("state") != "on":
        return []
    return ["--remote-control", decided["detail"], "--remote-control-session-name-prefix", name_prefix()]


def line(agent: Mapping) -> str:
    decided = agent.get("remote_control")
    return f"Remote Control: {decided['state']} ({decided['detail']})" if decided else ""


def failure(message: str) -> bool:
    return any(words in message for words in FAILURE_WORDS)


def record(store: RedisStore, slug: str, name: str, message: str, at: int) -> None:
    store.redis.hset(store.key(slug, FAILURES), name, json.dumps({"message": message, "at": at}))


def failures(store: RedisStore, slug: str) -> dict[str, dict]:
    return {name: json.loads(raw) for name, raw in store.redis.hgetall(store.key(slug, FAILURES)).items()}


def on_notification(payload: Mapping, environ: Mapping[str, str] = os.environ) -> bool:
    slug, name = environ.get("AGENTIHOOKS_SWARM"), environ.get("AGENTIHOOKS_AGENT_NAME")
    message = str(payload.get("message") or "")
    if not slug or not name or environ.get("AGENTIHOOKS_SWARM_LANE") != MASTER or not failure(message):
        return False
    record(RedisStore(connect()), slug, name, message, now_ms())
    return True
