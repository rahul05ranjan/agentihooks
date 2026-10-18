import json
import secrets

import pytest

from scripts.swarm import cli, lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import authority_context, ledger_authority, ledger_server
from scripts.swarm_v2 import masters
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")
live = _authority_live


@pytest.fixture
def pending(live, monkeypatch, tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, str(tmp_path), 1, 0, state="paused"))
    monkeypatch.setattr(lease, "now_ms", lambda *_: 10000)
    store.update(SLUG, pending_api_url=f"http://{live['host']}")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    actors = []
    for lane, seat in (("eng", "eng-1"), ("master", masters.seat(1)), ("master", masters.seat(2))):
        name = store.next_name(SLUG, lane)
        actors.append(
            store.start_execution(SLUG, AgentRecord(name, lane, "proof", seat=f"{seat}@{SLUG}", launched_at=10001))
        )
    secret = secrets.token_hex(32)
    monkeypatch.setattr(ledger_server.authority, "environ", {"SWARM_AUTH_MODE": "edge", "SWARM_SECRET": secret})
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda *_: store.redis)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    return store, actors, secret


def test_readiness_requires_each_current_worker_and_both_masters_to_authenticate(pending, monkeypatch, capsys):
    store, actors, secret = pending

    def report(actor, token):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
        monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", token)
        assert cli.main([SLUG, "ledger-ready"]) == 0
        return json.loads(capsys.readouterr().out)

    everyone = {actor.name for actor in actors}
    assert set(report(actors[0], "")["missing"]) == everyone
    wrong = ledger_authority.agent_token(secret, "foreign-ledger", actors[0].name)
    assert set(report(actors[0], wrong)["missing"]) == everyone
    for index, actor in enumerate(actors):
        token = ledger_authority.agent_token(secret, SLUG, actor.name)
        status = report(actor, token)
        assert set(status["missing"]) == {remaining.name for remaining in actors[index + 1 :]}
        assert status["ready"] is (index == len(actors) - 1)
    assert store.config(SLUG).api_url == ""
    assert authority_context.secret() is None


@pytest.mark.parametrize("command", ["ledger", "swarm"])
def test_normal_local_commands_acknowledge_the_staged_successor(pending, monkeypatch, capsys, command):
    from scripts.swarm_ledger import ledger

    _, actors, secret = pending
    actor = actors[0]
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(secret, SLUG, actor.name))
    if command == "ledger":
        monkeypatch.setattr(ledger, "cmd_url", lambda args: None)
        monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "url"])
        ledger.main()
    else:
        monkeypatch.setattr(cli, "cmd_status", lambda store, args: None)
        assert cli.main([SLUG, "status"]) == 0
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "")
    assert cli.main([SLUG, "ledger-ready"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["missing"]) == {master.name for master in actors[1:]}
    assert not report["ready"]
