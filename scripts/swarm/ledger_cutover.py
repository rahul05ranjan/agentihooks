import json
import os
from collections.abc import Mapping

from scripts.gates import Who
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_v2 import masters

READY = "ledger-ready"


def _stamp(config, agent):
    return json.dumps(
        {
            "url": config.pending_api_url,
            "staged_at": config.ledger_staged_at,
            "execution_id": agent.execution_id,
            "generation": agent.generation,
        }
    )


def _fresh(config, agent):
    return bool(agent.execution_id and agent.launched_at >= config.ledger_staged_at and agent.state != "finished")


def acknowledge(store: RedisStore, slug: str, who: Who, environ: Mapping[str, str] = os.environ) -> bool:
    token = environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN")
    if not who.pinned or not token or not store.redis.hget(store.key(slug, "config"), "pending_api_url"):
        return False
    config = store.config(slug)
    agent = next((agent for agent in store.agents(slug) if agent.name == who.name), None)
    if agent is None or not _fresh(config, agent):
        return False
    stamp = _stamp(config, agent)
    if store.redis.hget(store.key(slug, READY), who.name) == stamp:
        return True
    try:
        ResourceClient(config.pending_api_url, {"X-Ledger-Token": token, "X-Ledger-Agent": who.name}).request(
            slug, "metadata"
        )
    except OSError:
        return False
    from redis.exceptions import WatchError

    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(store.key(slug, "config"), store.key(slug, "executions"))
            settings = pipe.hgetall(store.key(slug, "config"))
            current = json.loads(pipe.hget(store.key(slug, "executions"), agent.execution_id) or "null")
            if (
                settings.get("pending_api_url") != config.pending_api_url
                or int(settings.get("ledger_staged_at") or 0) != config.ledger_staged_at
                or current is None
                or current["state"] == "finished"
                or current["generation"] != agent.generation
            ):
                return False
            pipe.multi()
            pipe.hset(store.key(slug, READY), who.name, stamp)
            pipe.execute()
            return True
        except WatchError:
            return False


def readiness(store: RedisStore, slug: str) -> dict:
    config = store.config(slug)
    agents = [agent for agent in store.agents(slug) if agent.state != "finished"]
    receipts = store.redis.hgetall(store.key(slug, READY))
    missing = sorted(
        agent.name for agent in agents if not _fresh(config, agent) or receipts.get(agent.name) != _stamp(config, agent)
    )
    expected = {f"{masters.seat(index)}@{slug}" for index in range(1, masters.MasterSeats(store.redis).count(slug) + 1)}
    absent = sorted(expected - {agent.seat for agent in agents if agent.lane == "master"})
    return {
        "ready": bool(config.pending_api_url) and not missing and not absent,
        "missing": missing,
        "missing_masters": absent,
        "pending_url": config.pending_api_url,
        "active_url": config.api_url,
        "staged_at": config.ledger_staged_at,
    }
