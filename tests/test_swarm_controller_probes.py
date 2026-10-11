import json

import pytest

from scripts.swarm import controller_probes
from scripts.swarm.controller import main

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
SLUG = "fixture"
ENV = {"AGENTIHOOKS_CONTROL_API_PORT": "8780", "AGENTIHOOKS_CONTROL_SWARM": SLUG}


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def store(server):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    found = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    found.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
    return found


class Fetch:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error


def held(store):
    from scripts.swarm import lease

    assert lease.acquire(store, SLUG, "hive-fixture") is not None


def test_live_passes_when_the_open_endpoint_answers_and_every_lease_is_held(store, capsys):
    held(store)
    fetch = Fetch()

    assert controller_probes.live(ENV, store, fetch) == 0
    assert fetch.calls == [("http://127.0.0.1:8780/v2/health/live", controller_probes.LIVE_TIMEOUT)]
    assert capsys.readouterr().out == "controller: live\n"


def test_live_fails_when_the_open_endpoint_does_not_answer(store, capsys):
    held(store)

    assert controller_probes.live(ENV, store, Fetch(OSError("connection refused"))) == 1
    assert capsys.readouterr().out == "controller: the health endpoint did not answer: OSError: connection refused\n"


def test_live_fails_when_a_swarm_has_no_live_controller_lease(store, capsys):
    fetch = Fetch()

    assert controller_probes.live(ENV, store, fetch) == 1
    assert capsys.readouterr().out == "controller: no live controller lease for ['fixture']\n"


def test_ready_passes_on_the_deep_report_while_redis_answers(store, capsys):
    assert controller_probes.ready(ENV, store, lambda: "brain_unreachable") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["probe"] == "deep"
    assert report["redis"] == "ok"
    assert report["brain"] == "unavailable"
    assert report["status"] == "degraded"


def test_ready_fails_when_redis_is_unavailable(store, server, capsys):
    server.connected = False

    assert controller_probes.ready(ENV, store, lambda: "ok") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["redis"] == "unavailable"
    assert report["brain"] == "ok"


@pytest.mark.parametrize("action", ["live", "ready"])
def test_the_controller_command_runs_each_probe(monkeypatch, action):
    calls = []
    monkeypatch.setattr("scripts.swarm.controller.connect", lambda: "store")
    monkeypatch.setattr(controller_probes, action, lambda environ, store: calls.append(store) or 3)

    assert main([action]) == 3
    assert calls == ["store"]
