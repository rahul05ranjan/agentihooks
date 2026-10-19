import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from scripts.swarm import cli, lease
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import authority_context, ledger, ledger_link, ledger_route
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run

pytestmark = pytest.mark.xdist_group("fakeredis")


def test_set_stores_the_swarm_api_address_and_clears_it(env, capsys, monkeypatch):
    from scripts.swarm import ledger_cutover

    store, _, _ = env
    monkeypatch.setattr(ledger_cutover, "settings", lambda store, slug, changes: store.update(slug, **changes))
    run("sw", "create", "--repo", "/repo")
    assert store.config("sw").api_url == ""
    assert run("sw", "set", "api-url=https://swarm.example.test:8443") == 0
    assert store.config("sw").api_url == "https://swarm.example.test:8443"
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["api_url"] == "https://swarm.example.test:8443"
    assert run("sw", "set", "api-url=http://swarm-api.agentihooks-swarm.svc:8780") == 0
    assert store.config("sw").api_url == "http://swarm-api.agentihooks-swarm.svc:8780"
    assert run("sw", "set", "api-url=") == 0
    assert store.config("sw").api_url == ""


@pytest.fixture
def store():
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("selected", "/repo", 0, 0, api_url="https://hosted.example"))
    result.create(SwarmConfig("other", "/repo", 0, 0))
    return result


def test_only_the_selected_tick_uses_the_hosted_ledger(store, monkeypatch):
    seen = {}
    before = dict(os.environ)
    monkeypatch.setattr(cli.outbox, "flush", lambda *_: None)
    monkeypatch.setattr(cli.relay, "relay", lambda *_: None)
    monkeypatch.setattr(cli.LaunchSlots, "refresh_reported", lambda *_: None)
    monkeypatch.setattr(cli.outage, "providers", lambda *_: None)
    monkeypatch.setattr(cli, "_publish_tick_status", lambda *_: None)
    monkeypatch.setattr(lease, "acquire", lambda *_: lease.Lease("host", 4, 10**15))
    from scripts.swarm import controller

    monkeypatch.setattr(controller, "take_tick_lock", lambda *_: "lock")
    monkeypatch.setattr(controller, "keep_tick", lambda *_: None)
    monkeypatch.setattr(controller, "release_tick_lock", lambda *_: None)

    def probe(slug, _now, _check):
        seen[slug] = (ledger.base(), ledger_link.remote())
        return False

    monkeypatch.setattr(cli.outage, "ledger_health", probe)
    with authority_context.bound("trusted-tick-fixture"), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda slug: cli.run_tick(store, slug, runtime=object()), ("selected", "other")))
    assert results == [["Ledger health probe failed"], ["Ledger health probe failed"]]
    assert seen == {
        "selected": ("https://hosted.example", True),
        "other": (ledger.BASE or ledger_link.base(), False),
    }
    assert dict(os.environ) == before


def test_a_pinned_native_ledger_command_resolves_its_own_swarm(store, monkeypatch):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "selected")
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
    monkeypatch.setattr(ledger, "cmd_url", lambda args: seen.append((args.slug, ledger.base(), ledger_link.remote())))
    with patch("sys.argv", ["ledger", "--slug", "selected", "url"]):
        ledger.main()
    assert seen == [("selected", "https://hosted.example", True)]
    assert not ledger_link.remote()


def test_a_native_remote_command_never_opens_a_workstation_store(monkeypatch):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "selected")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    monkeypatch.setenv("LEDGER_URL", "https://hosted.example")
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.setattr(ledger, "cmd_url", lambda args: seen.append(ledger.base()))

    def refuse():
        raise AssertionError("remote command opened the workstation store")

    monkeypatch.setattr("scripts.swarm.store.connect", refuse)
    with patch("sys.argv", ["ledger", "--slug", "selected", "url"]):
        ledger.main()
    assert seen == ["https://hosted.example"]


def test_scoped_routes_match_the_slug_and_restore_after_a_refusal():
    assert ledger_route.url("selected") == ""
    with ledger_route.scoped("selected", "https://hosted.example/"):
        assert ledger_route.url("selected") == "https://hosted.example"
        assert ledger_route.url("other") == ""
        assert ledger.base("selected") == "https://hosted.example"
        assert not ledger_link.remote(slug="other")
        with pytest.raises(ValueError):
            with ledger_route.scoped("other", "https://other.example"):
                assert ledger_route.url("selected") == ""
                raise ValueError("refused")
        assert ledger_route.url("selected") == "https://hosted.example"
    assert ledger_route.url() == ""


def test_pending_destination_stages_private_authority_without_routing_the_tick(store, monkeypatch):
    from argparse import Namespace

    from scripts import operator_env
    from scripts.swarm_ledger import authority_context
    from scripts.swarm_ledger.storage_migration import hosted

    store.update("selected", api_url="")
    monkeypatch.setattr(cli, "run_tick", lambda *_: [])
    cli.cmd_set(store, Namespace(slug="selected", pairs=["api-url-pending=https://hosted.example"]))
    config = store.config("selected")
    assert config.api_url == ""
    assert config.pending_api_url == "https://hosted.example"
    assert config.ledger_staged_at > 0
    assert store.config("other").pending_api_url == ""
    seen = []
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(operator_env, "fill", lambda *_: None)
    monkeypatch.setattr(operator_env, "classifier", lambda *_: None)
    monkeypatch.setattr(hosted, "server_secret", lambda: "private-staging-fixture")
    monkeypatch.setattr(cli, "_reconcile_services", lambda: None)

    def run(slugs, callback, settled, elapsed):
        seen.append((authority_context.secret(), ledger_link.remote()))
        return True

    monkeypatch.setattr(cli.tick_stop, "run", run)
    cli.cmd_tick(store, None)
    assert seen == [("private-staging-fixture", False)]
    assert "SWARM_SECRET" not in os.environ
    assert authority_context.secret() is None


def test_timer_prechecks_and_waits_resolve_the_selected_route(store, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_tickable", lambda slug: seen.append((slug, ledger.base())) or False)
    cli._tick_one(store, "selected")
    cli._tick_one(store, "other")
    assert seen == [("selected", "https://hosted.example"), ("other", ledger.BASE or ledger_link.base())]
    seen.clear()
    monkeypatch.setattr(lease, "acquire", lambda _store, slug, owner: seen.append((slug, ledger.base())))
    assert cli.run_waits(store, "selected") == []
    assert cli.run_waits(store, "other") == []
    assert seen == [("selected", "https://hosted.example"), ("other", ledger.BASE or ledger_link.base())]


@pytest.mark.parametrize("active", [False, True])
def test_an_agent_control_leaves_the_hosted_service_tick_to_its_trusted_runner(store, monkeypatch, active):
    from types import SimpleNamespace

    from scripts.swarm_ledger import authority_context

    monkeypatch.setattr(cli.Who, "from_env", lambda: SimpleNamespace(pinned=True, name="master@a1b2c3-0001"))
    store.update(
        "selected",
        api_url="https://hosted.example" if active else "",
        pending_api_url="https://hosted.example",
    )
    acquired = []
    monkeypatch.setattr(lease, "acquire", lambda *_: acquired.append(True))
    with authority_context.bound(None):
        actions = cli.run_tick(store, "selected", runtime=object())
    assert not acquired
    assert actions


def test_a_native_control_notice_uses_its_callers_own_authority(store, monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import ledger_client
    from scripts.swarm_ledger import ledger_authority

    master = "master@a1b2c3-0001"
    monkeypatch.setattr(cli.Who, "from_env", lambda: SimpleNamespace(pinned=True, name=master))
    delivered = []

    def call(slug, ops, service):
        assert slug == "selected"
        assert not ledger_authority.refusal(master, ops[0])
        assert not service
        delivered.append(ops[0]["text"])
        return {}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call, Missing=ledger.Missing))
    with ledger_route.scoped("selected", store.config("selected").api_url):
        ledger_client.LedgerClient().notify("selected", "The master paused the swarm")
    assert delivered == ["The master paused the swarm"]
