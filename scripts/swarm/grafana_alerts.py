"""Firing Grafana swarm bottleneck alerts in, standing stage findings out, read through the gateway Grafana tools.

Only numbers and known names are taken from an alert; its free text never reaches the swarm.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.swarm.health import standing
from scripts.swarm.langfuse_read import gateway_client

if TYPE_CHECKING:
    from redis import Redis

URL_ENV = "LANGFUSE_MCP_URL"
TOOL = "grafana-alerting_manage_rules"
TIMEOUT_S = 10
EPISODE_CHARS = 40
UNIT = "task hours in the window"
STAGES = (
    "seat_wait",
    "review",
    "merge_queue",
    "intent_fix",
    "launch_failure",
    "ci_queue",
    "quota_hold",
    "host_hold",
)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def _text(result) -> object:
    if result.isError:
        raise RuntimeError("Grafana alert rule tool failed")
    return json.loads(next(item.text for item in result.content if item.type == "text"))


async def _rules(timeout: float) -> list:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {
        "Authorization": f"Bearer {os.environ['MCP_KEY_GATEWAY']}",
        "Host": "llm.homeofanton.com",
        "x-mcp-servers": "grafana",
    }
    async with asyncio.timeout(timeout):
        async with streamablehttp_client(
            os.environ[URL_ENV], headers=headers, timeout=timeout, httpx_client_factory=gateway_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = _text(await session.call_tool(TOOL, {"operation": "list"})) or []
                return [
                    _text(await session.call_tool(TOOL, {"operation": "get", "rule_uid": rule["uid"]}))
                    for rule in listed
                    if (rule.get("labels") or {}).get("swarm") == "bottleneck" and rule.get("state") == "firing"
                ]


def read_rules(timeout: float = TIMEOUT_S) -> list:
    return asyncio.run(_rules(timeout))


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _firing(rule, slug: str) -> dict:
    if not isinstance(rule, dict):
        return {}
    labels = rule.get("labels") if isinstance(rule.get("labels"), dict) else {}
    stage, threshold = str(labels.get("stage")), _number(labels.get("threshold_hours"))
    if labels.get("swarm") != "bottleneck" or stage not in STAGES or threshold is None:
        return {}
    found = {}
    for alert in rule.get("alerts") or []:
        if not isinstance(alert, dict) or not str(alert.get("state")).startswith("Alerting"):
            continue
        tags = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        notes = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
        hours = _number(notes.get("hours"))
        if tags.get("ledger") == slug and SLUG.fullmatch(slug) and hours is not None:
            episode = str(alert.get("activeAt") or "")[:EPISODE_CHARS]
            found[stage] = standing.Reading(hours, threshold, UNIT, episode)
    return found


def apply(redis: Redis, slug: str, rules: list, now_ms: int) -> list[str]:
    firing = {}
    for rule in rules:
        firing.update(_firing(rule, slug))
    actions = []
    for stage, reading in firing.items():
        standing.fire(redis, slug, stage, reading, now_ms)
        actions.append(f"fired {stage} on {slug}")
    for stage, row in standing.read(redis, slug).items():
        if stage in STAGES and stage not in firing and row.get("unit") == UNIT and standing.clear(redis, slug, stage):
            actions.append(f"cleared {stage} on {slug}")
    return actions


def refresh(redis: Redis, slug: str, now_ms: int, read: Callable[[], list] = read_rules) -> list[str]:
    if not (os.environ.get(URL_ENV) and os.environ.get("MCP_KEY_GATEWAY")):
        return []
    try:
        rules = read()
    except Exception:
        return []
    return apply(redis, slug, rules, now_ms)
