import os

import pytest

from hooks.context import account_sessions, broadcast
from scripts.swarm import take_master
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm_ledger import ledger as remote
from scripts.swarm_ledger.api import client
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_take_master import taker  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

HIVE = "hive-ledger-credential"
CARRIED = "s-261013-101500"
MASTER = "master@a1b2c3-0001"


def _hive_client(sent):
    class HiveClient:
        def __init__(self, base, credentials, timeout=10):
            assert base == "https://hub.example"
            self.credentials = credentials

        def request(self, slug, path, payload=None):
            sent.append((slug, path, self.credentials))
            if path != "agent-token":
                return {"data": {}}
            return {"data": {"token": f"minted for {self.credentials['X-Ledger-Agent']}"}}

    return HiveClient


def _operator_token(slug):
    raise AssertionError("take-master read the operator's ledger credential")


@pytest.fixture
def remote_taker(taker, monkeypatch, tmp_path):  # noqa: F811
    for key, value in {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": "https://hub.example",
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": HIVE,
        "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "carried token",
        "AGENTIHOOKS_AGENT_NAME": CARRIED,
        "AGENTIHOOKS_SWARM": "",
        "AGENTIHOOKS_CONTROLLER_CREDENTIAL": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(broadcast, "BROADCAST_FILE", str(tmp_path / "broadcast.json"))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "name_session", broadcast.name_session)
    broadcast.register_session("session", 4242, "/repo", "sol", name=CARRIED)
    monkeypatch.setattr(remote.repository, "token", _operator_token)
    sent = []
    monkeypatch.setattr(client, "ResourceClient", _hive_client(sent))
    return (*taker, sent)


def test_take_master_on_a_remote_ledger_pins_the_new_master_before_its_first_ledger_call(remote_taker):
    store, ledger, _, _, sent = remote_taker
    seen = []
    ledger.closed = lambda slug: seen.append(remote.credentials(slug)) or False

    assert run("sw", "take-master") == 0

    pinned = {"X-Ledger-Token": f"minted for {MASTER}", "X-Ledger-Agent": MASTER}
    assert seen == [pinned]
    hive = {"X-Hive-Credential": HIVE, "X-Ledger-Agent": MASTER}
    assert sent[0] == ("sw", "agent-token", hive)
    tokens = [credentials for _, path, credentials in sent if path == "agent-token"]
    reads = [credentials for _, path, credentials in sent if path != "agent-token"]
    assert (tokens, reads) == ([hive] * len(tokens), [pinned] * len(reads))
    assert (os.environ["AGENTIHOOKS_SWARM"], os.environ["AGENTIHOOKS_AGENT_NAME"]) == ("sw", MASTER)
    assert "AGENTIHOOKS_LEDGER_AGENT_TOKEN" not in os.environ
    assert [a.name for a in store.agents("sw") if a.lane == "master"] == [MASTER]
    assert store.seats.occupant("master@sw").occupant == MASTER


def test_a_local_ledger_keeps_the_session_unpinned(taker, monkeypatch):  # noqa: F811
    _, ledger, _, _ = taker
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", CARRIED)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "carried token")
    assert run("sw", "take-master") == 0
    assert (os.environ["AGENTIHOOKS_SWARM"], os.environ["AGENTIHOOKS_AGENT_NAME"]) == ("", CARRIED)
    assert os.environ["AGENTIHOOKS_LEDGER_AGENT_TOKEN"] == "carried token"


def _refused(slug):
    raise LedgerRefused("ledger sw refused: unauthenticated")


def _unauthenticated(slug):
    raise remote.unauthenticated("the ledger server refused the credential")


def _taken(argv):
    try:
        return run(*argv)
    except SystemExit as exc:
        return str(exc)


@pytest.mark.parametrize(
    ("refusal", "outcome"),
    [(_refused, 1), (_unauthenticated, "unauthenticated: the ledger server refused the credential")],
)
def test_a_refused_take_master_saves_no_master_and_no_seat(taker, refusal, outcome):  # noqa: F811
    store, ledger, _, _ = taker
    ledger.closed = refusal
    assert _taken(["sw", "take-master"]) == outcome
    assert [a.name for a in store.agents("sw") if a.lane == "master"] == []
    assert store.seats.occupant("master@sw").occupant == ""


def test_a_refused_take_master_retires_no_live_master(taker):  # noqa: F811
    store, ledger, rt, _ = taker
    run("sw", "start")
    seated = store.seats.occupant("master@sw")
    ledger.closed = _refused
    assert run("sw", "take-master", "--replace") == 1
    assert rt.killed == []
    assert [a.name for a in store.agents("sw") if a.lane == "master"] == [MASTER]
    assert store.seats.occupant("master@sw") == seated
