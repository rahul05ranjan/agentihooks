import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from redis import Redis

from scripts.doctor.priming import doctor_slug
from scripts.swarm.store import PREFIX, RedisStore


def repository(repo: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return str(Path(result.stdout.strip()).resolve()) if result.returncode == 0 else str(Path(repo).resolve())


def key(repo: str) -> str:
    identity = sha256(repository(repo).encode()).hexdigest()
    return f"{PREFIX}:promotion-owner:{identity}"


def select(slug: str, repo: str, store: RedisStore) -> str:
    candidates = {
        name: config
        for name in store.slugs()
        if (config := store.config(name)).state == "running" and repository(config.repo) == repository(repo)
    }
    record = key(repo)
    previous = store.redis.hget(record, "owner")
    if previous in candidates:
        return previous
    preferred = [name for name in candidates if doctor_slug(store.peer(name) or "") != name]
    owner = min(preferred or candidates or [slug])
    store.redis.hset(record, mapping={"owner": owner, "previous": previous or "", f"swarm:{owner}": owner})
    return owner


def history(repo: str, redis: Redis) -> set[str]:
    return {value for field, value in redis.hgetall(key(repo)).items() if field.startswith("swarm:")}


@contextmanager
def intake(slug: str, repo: str, store: RedisStore) -> Iterator[str | None]:
    lock = store.redis.lock(f"{key(repo)}:lock", timeout=300)
    if not lock.acquire(blocking=False):
        yield None
        return
    try:
        yield select(slug, repo, store)
    finally:
        lock.release()
