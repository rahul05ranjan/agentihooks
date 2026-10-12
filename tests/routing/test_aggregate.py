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
