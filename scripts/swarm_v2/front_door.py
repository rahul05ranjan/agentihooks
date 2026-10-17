"""The front door master: the bare master seat alone faces the operator, in page chat and on the claude ai login.

Backend master seats reach the operator only through the front door, over the masters channel.
"""

from scripts.inbox.seats import seat_address
from scripts.swarm.store import MASTER

NAME = "front-door"
CHAT = "chat"
CHAT_WRITES = ("add", "edit")


def seat(slug: str) -> str:
    return seat_address(slug, MASTER)


def holds(slug: str, held: str) -> bool:
    """A master with no seat predates seats and holds the front door."""
    return held in ("", seat(slug))


def chat_write(op: dict) -> bool:
    return op.get("op") in CHAT_WRITES and op.get("thread") == CHAT and "by" in op


def refusal(slug: str, held: str) -> str:
    return (
        f"page chat belongs to the front door master {seat(slug)}, so {held} cannot write it; ask the front door "
        f'with agentihooks swarm {slug} masters-channel say "<text>"'
    )


def _connect():
    from scripts.swarm.store import redis_client

    return redis_client()


class Door:
    def __init__(self, slug: str, inner=None, connect=None, home=None) -> None:
        self.slug, self.inner, self.connect, self.home = slug, inner, connect or _connect, home

    def apply(self, doc, op, ctx, apply):
        return self._inner(doc, op, ctx, apply)

    def _inner(self, doc, op, ctx, apply):
        return self.inner.apply(doc, op, ctx, apply) if self.inner else apply(doc, op, ctx)
