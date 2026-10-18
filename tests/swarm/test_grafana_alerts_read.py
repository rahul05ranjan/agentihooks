import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from scripts.swarm import grafana_alerts

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]


RULE = {"uid": "a", "state": "firing", "labels": {"swarm": "bottleneck"}, "alerts": []}


def _gateway(monkeypatch, listed, details, error=False):
    import mcp
    from mcp.client import streamable_http

    calls = []

    @asynccontextmanager
    async def transport(url, headers, timeout, httpx_client_factory):
        from scripts.swarm.langfuse_read import gateway_client

        assert httpx_client_factory is gateway_client
        assert url == "http://fixture/mcp/"
        assert headers == {
            "Authorization": "Bearer fixture",
            "Host": "llm.homeofanton.com",
            "x-mcp-servers": "grafana",
        }
        assert timeout == 0.5
        yield "read", "write", None

    class Session:
        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            calls.append("initialize")

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            body = listed if arguments["operation"] == "list" else details[arguments["rule_uid"]]
            return SimpleNamespace(isError=error, content=[SimpleNamespace(type="text", text=json.dumps(body))])

    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    monkeypatch.setattr(streamable_http, "streamablehttp_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


def test_the_reader_gets_only_firing_bottleneck_rules_through_the_gateway(monkeypatch):
    listed = [
        {"uid": "a", "state": "firing", "labels": {"swarm": "bottleneck"}},
        {"uid": "b", "state": "normal", "labels": {"swarm": "bottleneck"}},
        {"uid": "c", "state": "firing", "labels": {"swarm": "other"}},
        {"uid": "d", "state": "firing"},
        {"uid": "e", "state": "pending", "labels": {"swarm": "bottleneck"}},
    ]
    calls = _gateway(monkeypatch, listed, {"a": RULE})
    assert grafana_alerts.read_rules(0.5) == [RULE]
    assert calls == [
        "initialize",
        ("grafana-alerting_manage_rules", {"operation": "list"}),
        ("grafana-alerting_manage_rules", {"operation": "get", "rule_uid": "a"}),
    ]


def test_the_reader_takes_an_empty_listing(monkeypatch):
    _gateway(monkeypatch, None, {})
    assert grafana_alerts.read_rules(0.5) == []


def test_the_reader_raises_on_a_tool_error(monkeypatch):
    _gateway(monkeypatch, [], {}, error=True)
    with pytest.raises(RuntimeError, match="^Grafana alert rule tool failed$"):
        grafana_alerts.read_rules(0.5)


def test_the_reader_defaults_to_a_ten_second_bound():
    assert grafana_alerts.TIMEOUT_S == 10
    assert grafana_alerts.read_rules.__defaults__ == (10,)
