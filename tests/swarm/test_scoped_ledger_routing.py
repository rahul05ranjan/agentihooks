import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from scripts.swarm import cli, lease
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger, ledger_link, ledger_route

pytestmark = pytest.mark.xdist_group("fakeredis")


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
    with ThreadPoolExecutor(max_workers=2) as pool:
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
