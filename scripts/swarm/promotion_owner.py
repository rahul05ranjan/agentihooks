from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from scripts.doctor.priming import doctor_slug
from scripts.swarm.store import PREFIX, RedisStore


def key(repo: str) -> str:
    identity = sha256(str(Path(repo).resolve()).encode()).hexdigest()
    return f"{PREFIX}:promotion-owner:{identity}"


def select(slug: str, repo: str, store: RedisStore) -> str:
    candidates = {
        name: config
        for name in store.slugs()
        if (config := store.config(name)).state == "running" and Path(config.repo).resolve() == Path(repo).resolve()
    }
    record = key(repo)
    previous = store.redis.hget(record, "owner")
    if previous in candidates:
        return previous
    preferred = [name for name in candidates if doctor_slug(store.peer(name) or "") != name]
    owner = min(preferred or candidates or [slug])
    store.redis.hset(record, mapping={"owner": owner, "previous": previous or ""})
    return owner


@contextmanager
def intake(slug: str, repo: str, store: RedisStore):
    lock = store.redis.lock(f"{key(repo)}:lock", timeout=300)
    if not lock.acquire(blocking=False):
        yield None
        return
    try:
        yield select(slug, repo, store)
    finally:
        lock.release()
