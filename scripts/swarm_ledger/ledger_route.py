import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

CURRENT: ContextVar[tuple[str, str] | None] = ContextVar("ledger_route", default=None)


def url(slug: str = "") -> str:
    route = CURRENT.get()
    return route[1] if route and (not slug or route[0] == slug) else ""


@contextmanager
def scoped(slug: str, address: str) -> Iterator[None]:
    token = CURRENT.set((slug, address.rstrip("/")))
    try:
        yield
    finally:
        CURRENT.reset(token)


@contextmanager
def session(slug: str) -> Iterator[None]:
    from scripts.swarm.store import connect

    if os.environ.get("AGENTIHOOKS_SWARM") == slug and not os.environ.get("SWARM_URL"):
        with scoped(slug, connect().config(slug).api_url):
            yield
    else:
        yield


def tick(function: Callable) -> Callable:
    @wraps(function)
    def routed(store, slug, *args, **kwargs):
        with scoped(slug, store.config(slug).api_url):
            return function(store, slug, *args, **kwargs)

    return routed
