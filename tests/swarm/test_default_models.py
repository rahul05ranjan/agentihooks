import argparse
import json
from dataclasses import replace
from unittest.mock import Mock

import fakeredis
import pytest

from scripts.swarm import cli, model_catalog, model_pick, runtime
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger_server

pytestmark = pytest.mark.xdist_group("fakeredis")
CATALOG = {
    "claude": [{"model": "opus"}, {"model": "sonnet"}],
    "codex": [{"model": "gpt-6.1-sol"}, {"model": "gpt-6-sol"}],
}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(model_catalog, "catalog", lambda *args: CATALOG)
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", 2, 1, state="paused"))
    return saved


def test_model_defaults_and_legacy_config(store):
    config = store.config("sw")
    assert (config.claude_model, config.codex_model) == ("opus", "gpt-6.1-sol")
    store.redis.hdel(store.key("sw", "config"), "claude_model", "codex_model")
    assert store.config("sw") == config


def test_set_persists_and_reports_both_models(store, capsys):
    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["claude-model=sonnet", "codex-model=gpt-6-sol"]))
    config = store.config("sw")
    assert (config.claude_model, config.codex_model) == ("sonnet", "gpt-6-sol")
    output = json.loads(capsys.readouterr().out)
    assert (output["claude_model"], output["codex_model"]) == ("sonnet", "gpt-6-sol")


@pytest.mark.parametrize("field", ["claude_model", "codex_model"])
@pytest.mark.parametrize("value", ["unknown", "", None, True, ["opus"]])
def test_store_refuses_unknown_models_without_writing(store, field, value):
    before = store.config("sw")
    with pytest.raises(SwarmError, match="catalog"):
        store.update("sw", **{field: value})
    assert store.config("sw") == before


def test_cli_refuses_a_model_from_the_other_harness(store):
    with pytest.raises(SwarmError, match="catalog"):
        cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["claude-model=gpt-6-sol"]))


def test_empty_catalog_refuses_a_new_model_but_allows_other_changes(store, monkeypatch):
    monkeypatch.setattr(model_catalog, "catalog", lambda *args: {"claude": [], "codex": []})
    assert store.update("sw", max_eng=3).max_eng == 3
    with pytest.raises(SwarmError, match="catalog"):
        store.update("sw", claude_model="sonnet")


def test_page_set_maps_catalog_models_and_refuses_unknown(monkeypatch):
    monkeypatch.setattr(ledger_server, "swarm_models", lambda: CATALOG)
    assert ledger_server.control_argv({"action": "set", "claude_model": "sonnet", "codex_model": "gpt-6-sol"}) == [
        "set",
        "claude-model=sonnet",
        "codex-model=gpt-6-sol",
    ]
    for field in ("claude_model", "codex_model"):
        with pytest.raises(ValueError, match="catalog"):
            ledger_server.control_argv({"action": "set", field: "unknown"})


@pytest.mark.parametrize("agent,model", [("claude", "sonnet"), ("codex", "gpt-6-sol")])
def test_configured_model_is_used_in_auto_band_and_frontier(agent, model, monkeypatch):
    monkeypatch.setattr(model_pick.runner, "run", Mock(side_effect=model_pick.ClassifierUnavailable("offline")))
    picked = model_pick.pick(agent, {"effort": "auto"}, {"difficulty": "S", "rank": "normal"}, {}, default_model=model)
    assert (picked.model, picked.effort) == (model, "medium")
    assert model_pick.frontier(agent, default_model=model).model == model


def test_launch_defaults_keep_explicit_choices(store):
    config = replace(store.config("sw"), claude_model="sonnet", codex_model="gpt-6-sol")
    for lane in ("eng", "ci", "plan", "master"):
        picked = runtime._lane_default(lane, "claude", {}, config)
        assert picked.model == "sonnet"
    assert runtime._lane_default("plan", "claude", {"model": "opus"}, config).model == "opus"
