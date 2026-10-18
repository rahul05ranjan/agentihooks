import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from scripts.swarm.store import RedisStore

CURRENT: ContextVar[tuple[str, str] | None] = ContextVar("ledger_route", default=None)


def configured(store: RedisStore, slug: str) -> str:
    return store.redis.hget(store.key(slug, "config"), "api_url") or ""


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

    if (
        os.environ.get("AGENTIHOOKS_SWARM") == slug
        and os.environ.get("AGENTIHOOKS_DEPLOYMENT", "local") == "local"
        and not os.environ.get("SWARM_URL")
    ):
        store = connect()
        with scoped(slug, store.config(slug).api_url):
            yield
            from scripts.gates import Who
            from scripts.swarm import ledger_cutover

            ledger_cutover.acknowledge(store, slug, Who.from_env())
    else:
        yield


def tick(function: Callable) -> Callable:
    @wraps(function)
    def routed(store, slug, *args, **kwargs):
        with scoped(slug, configured(store, slug)):
            return function(store, slug, *args, **kwargs)

    return routed


def service(function: Callable) -> Callable:
    @tick
    @wraps(function)
    def trusted(store, slug, *args, **kwargs):
        from scripts.swarm_ledger.authority_context import tick_headers

        pending = store.redis.hget(store.key(slug, "config"), "pending_api_url")
        if (url(slug) or pending) and not tick_headers(slug):
            return ["the host tick will apply the settings"]
        return function(store, slug, *args, **kwargs)

    return trusted
