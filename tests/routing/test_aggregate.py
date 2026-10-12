import json

import pytest

from scripts.hive import registry
from scripts.routing import aggregate

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def report(redis, hive, counts, observed, used=20):
    registry.update(redis, hive, ["max-agents=8"])
    redis.hset(
        registry.key(hive),
        mapping={
            "heartbeat_at": 100000,
            "slots": json.dumps(
                [
                    {"harness": "claude", "account": "one", "kind": "subscription", "cap": 6},
                    {"harness": "codex", "account": "one", "kind": "subscription", "cap": 6},
                ]
            ),
        },
    )
    redis.set(f"{registry.key(hive)}:sessions", json.dumps(counts), ex=45)
    redis.set(
        f"{registry.key(hive)}:quota",
        json.dumps(
            {
                "claude:one": {
                    "five_hour": {"used": used, "resets_at": 200},
                    "seven_day": {"used": 30, "resets_at": 300},
                    "observed_at": observed,
                },
            }
        ),
        ex=45,
    )


def test_aggregate_sums_hives_and_selects_freshest_slot_quota(redis):
    report(redis, "a", {"claude:one": 2, "codex:one": 1}, 90)
    report(redis, "b", {"claude:one": 3, "codex:one": 2}, 95, used=40)
    rows = aggregate.read(redis, 100)
    assert rows["claude:one"]["sessions"] == 5
    assert rows["codex:one"]["sessions"] == 3
    assert rows["claude:one"]["quota"] == {
        "five_hour": {"used": 40, "resets_at": 200},
        "seven_day": {"used": 30, "resets_at": 300},
        "observed_at": 95,
    }
    assert rows["codex:one"]["quota"] is None


def test_nonlocal_consumers_use_aggregate_without_credential_probes(redis, monkeypatch):
    from hooks.context import account_sessions
    from scripts.swarm import capacity

    report(redis, "a", {"claude:one": 2, "codex:one": 1}, 90)
    report(redis, "b", {"claude:one": 1, "codex:one": 2}, 95)
    monkeypatch.setenv("AGENTIHOOKS_RUNTIME_BACKEND", "kubernetes")
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    monkeypatch.setattr("time.time", lambda: 100)

    def forbidden(*args, **kwargs):
        raise AssertionError("controller must use hive quota reports")

    monkeypatch.setattr(capacity.balancer.ClaudeTokenSource, "results", forbidden)
    monkeypatch.setattr(capacity.codex_router.CodexAccountSource, "readings", forbidden)
    assert account_sessions.sessions_by_account() == {"one": 3}
    assert account_sessions.codex_sessions_by_account() == {"one": 3}
    rows = capacity.accounts({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}, 100)
    claude = next(row for row in rows if row.harness == "claude")
    assert (claude.sessions, claude.five_left, claude.week_left, claude.cap) == (3, 80, 70, 6)


def test_aggregate_includes_reservations_once_and_exit_releases(redis, monkeypatch):
    from scripts.routing import reservation

    monkeypatch.setattr("time.time", lambda: 100)
    report(redis, "a", {"claude:one": 0}, 95)
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 1
    assert slots.acknowledge("first")
    redis.set(
        f"{registry.key('a')}:sessions",
        json.dumps(
            {
                "claude:one": 1,
                "_executions": {"claude:one": ["first"]},
            }
        ),
        ex=45,
    )
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 1
    assert not slots.reserve("claude:one", "second", 1, 1000)
    assert slots.release("first")
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 0
    assert slots.reserve("claude:one", "second", 1, 1000)
