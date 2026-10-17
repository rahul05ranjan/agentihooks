import json
import subprocess

import pytest

from scripts.swarm import cli, commands, quota_refresh, timer
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig, SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    return saved


@pytest.fixture
def fresh(monkeypatch):
    from scripts import agents_quota

    page = {"probed_at": 50, "rows": ["fresh"]}
    monkeypatch.setattr(agents_quota, "page_quota", lambda: page)
    return page


def test_the_wake_channel_and_page_lifetime():
    assert quota_refresh.WAKE == f"{PREFIX}:quota-refresh"
    assert (quota_refresh.COMMAND, quota_refresh.WAIT_S, quota_refresh.PAGE_TTL_S) == ("quota", 30, 300)


def test_a_request_queues_one_quota_row_and_wakes_the_prober_at_once(store):
    pubsub = store.redis.pubsub()
    pubsub.subscribe(quota_refresh.WAKE)
    assert pubsub.get_message(timeout=1)["type"] == "subscribe"
    row = quota_refresh.request(store, "sw")
    assert [(r["id"], r["command"], r["argv"], r["state"]) for r in commands.rows(store, "sw")] == [
        (row["id"], "quota", [], "pending")
    ]
    message = pubsub.get_message(timeout=1)
    assert (message["channel"], message["data"]) == (quota_refresh.WAKE, "sw")


def test_answer_probes_only_the_quota_rows_and_writes_the_fresh_page_quota(store, fresh):
    commands.submit(store, "sw", "swarm", ["pause"])
    quota_refresh.request(store, "sw")
    ran = []
    actions = quota_refresh.answer(store, "sw", lambda slug, row: ran.append((slug, row["command"])) or "")
    assert actions == ["control quota acknowledged"]
    assert ran == [("sw", "quota")]
    assert [(r["command"], r["state"]) for r in commands.rows(store, "sw")] == [
        ("swarm", "pending"),
        ("quota", "acknowledged"),
    ]
    key = quota_refresh.page_key(store, "sw")
    assert key == f"{PREFIX}:sw:quota-page"
    assert json.loads(store.redis.get(key)) == fresh
    assert 299 <= store.redis.ttl(key) <= 300


def test_a_failed_probe_is_recorded_and_leaves_the_page_quota_alone(store, fresh):
    quota_refresh.request(store, "sw")
    assert quota_refresh.answer(store, "sw", lambda slug, row: "probe failed") == ["control quota failed"]
    assert commands.rows(store, "sw")[0]["error"] == "probe failed"
    assert store.redis.get(quota_refresh.page_key(store, "sw")) is None


def test_answer_without_a_pending_quota_row_writes_nothing(store, fresh):
    commands.submit(store, "sw", "swarm", ["pause"])
    assert quota_refresh.answer(store, "sw", lambda slug, row: pytest.fail("ran a row")) == []
    assert store.redis.get(quota_refresh.page_key(store, "sw")) is None


def test_the_newer_probe_wins_and_a_missing_side_yields_the_other():
    old, new, bare = {"probed_at": 10}, {"probed_at": 20}, {}
    assert quota_refresh.fresher(old, None) is old
    assert quota_refresh.fresher(None, new) is new
    assert quota_refresh.fresher(None, None) is None
    assert quota_refresh.fresher(old, new) is new
    assert quota_refresh.fresher(new, old) is new
    assert quota_refresh.fresher(new, {"probed_at": 20}) == {"probed_at": 20}
    assert quota_refresh.fresher(new, {"probed_at": 20}) is not new
    assert quota_refresh.fresher(bare, {"probed_at": None}) == {"probed_at": None}
    assert quota_refresh.fresher(new, bare) is new
    assert quota_refresh.fresher({"probed_at": None}, {"probed_at": 1}) == {"probed_at": 1}
    unread, probe = {"probed_at": None}, {"probed_at": None, "rows": ["probe"]}
    assert quota_refresh.fresher(unread, probe) is probe
    assert quota_refresh.fresher({"probed_at": 1}, unread) == {"probed_at": 1}


def test_the_status_view_carries_the_newer_of_the_published_and_the_probed_quota(store):
    commands.publish(store, "sw", {"agents": [], "tasks": {}, "quota": {"probed_at": 10, "rows": ["tick"]}}, {})
    assert commands.view(store, "sw")["quota"]["rows"] == ["tick"]
    store.redis.set(quota_refresh.page_key(store, "sw"), json.dumps({"probed_at": 20, "rows": ["probe"]}))
    assert commands.view(store, "sw")["quota"]["rows"] == ["probe"]
    commands.publish(store, "sw", {"agents": [], "tasks": {}, "quota": {"probed_at": 30, "rows": ["later"]}}, {})
    assert commands.view(store, "sw")["quota"]["rows"] == ["later"]


def test_the_status_view_without_any_quota_adds_none(store):
    commands.publish(store, "sw", {"agents": [], "tasks": {}}, {})
    assert "quota" not in commands.view(store, "sw")


def test_answer_all_reports_every_swarm_and_carries_on_past_a_refused_one(store, monkeypatch, capsys):
    store.create(SwarmConfig("two", ".", 0, 0, state="paused"))

    def answer(store, slug, execute):
        if slug == "sw":
            raise SwarmError("the controller lease is stale")
        return ["control quota acknowledged"]

    monkeypatch.setattr(quota_refresh, "answer", answer)
    lines = quota_refresh.answer_all(store, lambda slug, row: "")
    assert lines == ["sw: the controller lease is stale", "two: control quota acknowledged"]
    assert capsys.readouterr().out == "sw: the controller lease is stale\ntwo: control quota acknowledged\n"


class Stop(Exception):
    pass


def test_run_listens_on_the_wake_channel_and_answers_every_swarm_on_each_wake(store, monkeypatch):
    seen, rounds = [], []

    class PubSub:
        def subscribe(self, channel):
            seen.append(("subscribe", channel))

        def get_message(self, timeout):
            seen.append(("wait", timeout))

    def answer_all(store_, execute):
        rounds.append(store_)
        if len(rounds) == 2:
            raise Stop

    monkeypatch.setattr(store.redis, "pubsub", PubSub)
    monkeypatch.setattr(quota_refresh, "answer_all", answer_all)
    with pytest.raises(Stop):
        quota_refresh.run(store, lambda slug, row: "", 7)
    assert seen == [("subscribe", quota_refresh.WAKE), ("wait", 7), ("wait", 7)]
    assert rounds == [store, store]


def test_timer_ensure_also_runs_the_quota_prober_service(tmp_path):
    calls = []
    timer.ensure(
        "/bin/agentihooks",
        tmp_path,
        run=lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0),
    )
    assert (tmp_path / "agentihooks-quota-prober.service").read_text() == (
        "[Unit]\nDescription=agentihooks quota prober for the ledger page refresh\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        'ExecStart="/bin/agentihooks" swarm prober\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    enable = ["systemctl", "--user", "enable", "--now", "agentihooks-quota-prober.service"]
    assert (enable, {"capture_output": True, "text": True, "timeout": 30}) in calls


def test_main_runs_the_quota_prober_without_a_slug_on_the_hive_command_runner(monkeypatch):
    from scripts.swarm import command_runner

    ran, executed = [], []
    monkeypatch.setattr(cli, "connect", lambda: "store")
    monkeypatch.setattr(quota_refresh, "run", lambda store, execute, wait_s: ran.append((store, wait_s, execute)))
    monkeypatch.setattr(command_runner, "execute", lambda store, slug, row: executed.append((store, slug, row)) or "")
    assert cli.main(["prober"]) == 0
    [(store, wait_s, execute)] = ran
    assert (store, wait_s) == ("store", 30)
    assert execute("sw", {"command": "quota"}) == ""
    assert executed == [("store", "sw", {"command": "quota"})]
