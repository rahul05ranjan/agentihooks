import pytest

from hooks import hook_manager
from scripts.swarm import remote_control
from scripts.swarm.store import RedisStore
from tests.swarm.test_cli import env as env

pytestmark = pytest.mark.xdist_group("fakeredis")

MASTER_ENV = {
    "AGENTIHOOKS_SWARM": "proof",
    "AGENTIHOOKS_AGENT_NAME": "master@a1b2c3-0001",
    "AGENTIHOOKS_SWARM_LANE": "master",
}
ON = {"state": "on", "detail": "master · sw"}


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(remote_control, "connect", lambda environ=None: redis)
    monkeypatch.setattr(remote_control, "now_ms", lambda: 12_500)
    return RedisStore(redis)


def test_a_master_failure_notice_is_recorded_on_its_agent(store):
    assert remote_control.on_notification({"message": "Remote Control failed · /remote-control"}, MASTER_ENV) is True
    assert remote_control.failures(store, "proof") == {
        "master@a1b2c3-0001": {"message": "Remote Control failed · /remote-control", "at": 12_500}
    }


def test_the_notice_reads_the_process_environment_by_default(store, monkeypatch):
    for key, value in MASTER_ENV.items():
        monkeypatch.setenv(key, value)
    assert remote_control.on_notification({"message": "Remote Control failed"}) is True
    assert list(remote_control.failures(store, "proof")) == ["master@a1b2c3-0001"]


def test_the_clock_is_milliseconds(monkeypatch):
    monkeypatch.setattr(remote_control.time, "time", lambda: 12.5)
    assert remote_control.now_ms() == 12_500


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


def test_the_notification_hook_hands_its_payload_to_the_recorder(monkeypatch):
    seen = []
    monkeypatch.setattr(remote_control, "on_notification", lambda payload: seen.append(payload))
    hook_manager.on_notification({"message": "Remote Control failed"})
    assert seen == [{"message": "Remote Control failed"}]


def test_a_recorder_failure_is_logged_and_never_raised(monkeypatch):
    logged = []

    def broken(payload):
        raise RuntimeError("redis down")

    monkeypatch.setattr(remote_control, "on_notification", broken)
    monkeypatch.setattr(hook_manager, "log", lambda message, data=None: logged.append((message, data)))
    hook_manager.on_notification({"message": "Remote Control failed"})
    assert logged[-1] == ("remote control notice failed", {"error": "redis down"})


def test_health_rows_carry_the_recorded_failure(store, monkeypatch):
    from scripts.swarm import status
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr(status.activity, "last_events", lambda slug: {})
    remote_control.record(store, "proof", "master@a1b2c3-0001", "Remote Control failed", 7)
    agents = [AgentRecord("master@a1b2c3-0001", "master", "master"), AgentRecord("engineer@a1b2c3-0002", "eng", "t1")]
    rows = status._health_rows(store, "proof", agents, {}, 10**13)
    assert [row["remote_control_failure"] for row in rows] == [{"message": "Remote Control failed", "at": 7}, None]


def test_status_text_and_json_show_the_master_remote_control_line(env, capsys):
    from types import SimpleNamespace

    from scripts.swarm import cli, status
    from scripts.swarm.store import AgentRecord

    store, _, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    master = AgentRecord("master@a1b2c3-0001", "master", "master", harness="claude", remote_control=ON)
    engineer = AgentRecord("engineer@a1b2c3-0002", "eng", "t1", harness="claude")
    store.put_agent("sw", master)
    store.put_agent("sw", engineer)
    rt.live = {master.name, engineer.name}
    shown = {a["name"]: a["remote_control"] for a in status.status_report(store, "sw", {"tasks": []}, rt)["agents"]}
    assert shown == {master.name: ON, engineer.name: {}}
    assert cli._remote_control_text([master, engineer]) == "Remote Control: on (master · sw)\n"
    capsys.readouterr()
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    assert "\nRemote Control: on (master · sw)\n" in capsys.readouterr().out
