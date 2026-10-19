from collections.abc import Callable
from dataclasses import dataclass

from scripts.swarm import outage

REPORT_MINUTES, PARK_MINUTES, GRACE_MINUTES = 90, 150, 15
SENDER, KEY, CAUSE = "swarm", "overrun", "overrun"
HELD = ("claimed", "pr")
NO_STEPS = "no steps recorded in progress.md"


@dataclass(frozen=True)
class Watch:
    store: object
    slug: str
    ledger: object
    live: frozenset
    retire: Callable
    now_ms: int


def overrun_pass(watch, doc):
    outage.held(watch.store, watch.slug, None)
    return []
