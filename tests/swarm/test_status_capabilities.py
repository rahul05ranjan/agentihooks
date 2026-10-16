from types import SimpleNamespace

import pytest

from scripts.swarm import cli, status
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def saved(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/r", 1, 0))
    store.put_agent("sw", AgentRecord("c", "eng", "t1", harness="claude"))
    store.put_agent("sw", AgentRecord("p", "eng", "t2", harness="codex", transport=APP_SERVER_TRANSPORT))
    store.put_agent("sw", AgentRecord("l", "eng", "t3", harness="codex"))
    store.put_agent("sw", AgentRecord("u", "ci", "t4", harness=""))
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: {"c", "p", "l", "u"})
    return store


def test_status_report_carries_each_agents_transport_capabilities(saved):
    report = status.status_report(saved, "sw", {"tasks": []})
    assert {a["name"]: a["capabilities"] for a in report["agents"]} == {
        "c": {"push_inbox": True, "native_terminal_attach": False},
        "p": {"push_inbox": True, "native_terminal_attach": True},
        "l": {"push_inbox": False, "native_terminal_attach": False},
        "u": {"push_inbox": False, "native_terminal_attach": False},
    }


def test_status_text_prints_push_inbox_and_native_terminal_per_agent(saved, capsys):
    cli.cmd_status(saved, SimpleNamespace(slug="sw", json=False))
    rows = [line.split("\t") for line in capsys.readouterr().out.splitlines() if "\t" in line]
    head = rows[0]
    push, attach = head.index("Push inbox"), head.index("Native terminal")
    agents = {row[0]: (row[push], row[attach]) for row in rows[1:]}
    assert agents == {"c": ("yes", "no"), "p": ("yes", "yes"), "l": ("no", "no"), "u": ("no", "no")}
