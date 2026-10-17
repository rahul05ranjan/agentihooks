import pytest

from scripts.swarm import remote_control
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")

MASTER_ENV = {
    "AGENTIHOOKS_SWARM": "proof",
    "AGENTIHOOKS_AGENT_NAME": "master@a1b2c3-0001",
    "AGENTIHOOKS_SWARM_LANE": "master",
}


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    from scripts.swarm import store as store_module

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store_module, "connect", lambda environ=None: redis)
    monkeypatch.setattr(remote_control.time, "time", lambda: 12.5)
    return RedisStore(redis)


def test_a_master_failure_notice_is_recorded_on_its_agent(store):
    assert remote_control.on_notification({"message": "Remote Control failed · /remote-control"}, MASTER_ENV) is True
    assert remote_control.failures(store, "proof") == {
        "master@a1b2c3-0001": {"message": "Remote Control failed · /remote-control", "at": 12_500}
    }


@pytest.mark.parametrize(
    ("payload", "environ"),
    [
        ({"message": "Remote Control failed"}, {**MASTER_ENV, "AGENTIHOOKS_SWARM": ""}),
        ({"message": "Remote Control failed"}, {**MASTER_ENV, "AGENTIHOOKS_AGENT_NAME": ""}),
        ({"message": "Remote Control failed"}, {**MASTER_ENV, "AGENTIHOOKS_SWARM_LANE": "eng"}),
        ({"message": "Claude is waiting for your input"}, MASTER_ENV),
        ({}, MASTER_ENV),
        ({"message": None}, MASTER_ENV),
    ],
    ids=["no-swarm", "no-name", "worker", "other-notice", "no-message", "null-message"],
)
def test_other_sessions_and_notices_record_nothing(store, payload, environ):
    assert remote_control.on_notification(payload, environ) is False
    assert remote_control.failures(store, "proof") == {}


def test_the_latest_notice_replaces_the_earlier_one(store):
    remote_control.record(store, "proof", "m", "Remote Control failed", 1)
    remote_control.record(store, "proof", "m", "Remote Control disconnected", 2)
    assert remote_control.failures(store, "proof") == {"m": {"message": "Remote Control disconnected", "at": 2}}


def test_health_rows_carry_the_recorded_failure(store, monkeypatch):
    from scripts.swarm import status
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr(status.activity, "last_events", lambda slug: {})
    remote_control.record(store, "proof", "master@a1b2c3-0001", "Remote Control failed", 7)
    agents = [AgentRecord("master@a1b2c3-0001", "master", "master"), AgentRecord("engineer@a1b2c3-0002", "eng", "t1")]
    rows = status._health_rows(store, "proof", agents, {}, 10**13)
    assert [row["remote_control_failure"] for row in rows] == [{"message": "Remote Control failed", "at": 7}, None]
