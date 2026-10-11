from dataclasses import replace

import fakeredis
import pytest

from scripts.swarm import outage, pane
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("harness", "text", "match"),
    [
        ("claude", "\x1b[31mAPI Error: 529\x1b[0m overloaded", "API Error: 529"),
        ("codex", "stream disconnected before completion: transport error", "stream disconnected before completion"),
        ("copilot", "Request failed with status code 503", "Request failed with status code 503"),
        ("claude", "API Error: 401 Unauthorized", ""),
        ("codex", "unexpected status 429 Too Many Requests", ""),
        ("unknown", "API Error: 529", ""),
    ],
)
def test_provider_errors_are_matched_only_for_their_harness(harness, text, match):
    assert pane.provider_error(harness, text) == match


def test_runtime_carries_provider_evidence_and_closes_after_a_successful_pane_read(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo"))
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="claude")
    store.put_agent("sw", agent)
    text = "API Error: 503 Service Unavailable"

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": "idle"}
        return {"text": text}

    runtime = HerdrRuntime(herdr=herdr)
    observed = runtime.observe(agent)
    assert observed.provider_error == "API Error: 503"
    assert observed.provider_checked
    outage.providers(store, "sw", runtime, 1000)
    assert outage.open_records("sw")[0]["evidence"] == ["worker (claude): API Error: 503"]
    text = "Ready for input"
    outage.providers(store, "sw", runtime, 2000)
    assert outage.read("sw")["provider"]["ended_at"] == 2000


def test_an_unreadable_or_remote_pane_cannot_clear_a_provider_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo"))
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1")
    store.put_agent("sw", agent)
    outage.observe("sw", "provider", ["worker: connection error"], 1000)

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": "idle"}
        raise OSError("pane unavailable")

    runtime = HerdrRuntime(herdr=herdr)
    assert not runtime.observe(agent).provider_checked
    assert not runtime.observe(replace(agent, runtime_backend="kubernetes")).provider_checked
    outage.providers(store, "sw", runtime, 2000)
    assert outage.read("sw")["provider"]["ended_at"] is None
