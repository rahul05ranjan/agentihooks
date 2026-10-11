import fcntl
import json
from dataclasses import replace
from functools import wraps
from pathlib import Path

from redis.exceptions import RedisError

from scripts.swarm.store import SwarmError
from scripts.swarm_v2.records import _write

HOME = None
PANE = "provider-error"
PANE_TTL_S = 24 * 3600
HELD = {"idle with claim": "idle during outage", "stale claim": "stale during outage"}


def home() -> Path:
    return HOME if HOME is not None else Path.home() / ".agentihooks" / "swarm"


def read(slug: str) -> dict:
    path = home() / slug / "outages.json"
    return json.loads(path.read_text()) if path.exists() else {}


def observe(slug: str, kind: str, evidence: list[str], now_ms: int) -> None:
    folder = home() / slug
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


def mark_pane(store, slug: str, name: str, error: str) -> None:
    store.redis.set(store.key(slug, PANE, name), error, ex=PANE_TTL_S)


def held(store, slug: str, name: str) -> str:
    """The evidence holding an agent: the swarm's open outages, else a provider error on its own pane."""
    evidence = [line for record in open_records(slug) for line in record["evidence"]]
    return "; ".join(evidence) or store.redis.get(store.key(slug, PANE, name)) or ""


def paused_ms(slug: str, since_ms: int, now_ms: int) -> int:
    spans = sorted(
        (max(record["started_at"], since_ms), now_ms if record["ended_at"] is None else min(record["ended_at"], now_ms))
        for record in read(slug).values()
    )
    total, reach = 0, since_ms
    for start, end in spans:
        total += max(0, end - max(start, reach))
        reach = max(reach, end)
    return total


def hold_findings(store, slug: str, found: list, agents: list) -> list:
    claimants = {agent.task: agent.name for agent in agents}
    return [_hold(store, slug, finding, claimants) for finding in found]


def _hold(store, slug, finding, claimants):
    if finding.kind not in HELD:
        return finding
    name = finding.subject if finding.kind == "idle with claim" else claimants.get(finding.subject, "")
    evidence = held(store, slug, name)
    if not evidence:
        return finding
    return replace(
        finding,
        kind=HELD[finding.kind],
        summary=f"{finding.summary}, held by an outage",
        evidence=(*finding.evidence, f"outage: {evidence}"),
    )


def providers(store, slug: str, runtime, now_ms: int) -> None:
    observed = [(agent, runtime.observe(agent)) for agent in store.agents(slug) if agent.state != "finished"]
    evidence = [
        f"{agent.name} ({agent.harness}): {pane.provider_error}" for agent, pane in observed if pane.provider_error
    ]
    if evidence or (observed and all(pane.provider_checked for _, pane in observed)):
        observe(slug, "provider", evidence, now_ms)


def redis_state(evidence: list[str], now_ms: int) -> None:
    root = home()
    slugs = [path.name for path in root.iterdir() if path.is_dir()] if root.exists() else []
    for slug in slugs or ["_controller"]:
        observe(slug, "redis", evidence, now_ms)


def connect_tick(connector, clock):
    try:
        store = connector()
    except (SwarmError, RedisError) as exc:
        evidence = f"Redis connect check failed: {type(exc).__name__}"
        redis_state([evidence], clock())
        print(evidence)
        return None
    redis_state([], clock())
    return store


def guard_tick(function):
    @wraps(function)
    def guarded(store, slug, *args, **kwargs):
        from scripts.swarm.cli import now_ms

        try:
            store.redis.ping()
        except RedisError as exc:
            evidence = f"Redis ping check failed: {type(exc).__name__}"
            observe(slug, "redis", [evidence], now_ms())
            return [evidence]
        observe(slug, "redis", [], now_ms())
        try:
            return function(store, slug, *args, **kwargs)
        except RedisError as exc:
            evidence = f"Redis operation failed: {type(exc).__name__}"
            observe(slug, "redis", [evidence], now_ms())
            return [evidence]

    return guarded
