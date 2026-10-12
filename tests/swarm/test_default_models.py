import argparse
import json
from dataclasses import replace
from unittest.mock import Mock

import fakeredis
import pytest

from scripts.swarm import cli, default_models, model_catalog, model_pick, runtime
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


@pytest.mark.parametrize("field", ["claude_model", "codex_model"])
def test_create_refuses_custom_unknown_models(store, field):
    with pytest.raises(SwarmError, match="catalog"):
        store.create(SwarmConfig("new", "/repo", 2, 1, **{field: "unknown"}))
    assert store.redis.hgetall(store.key("new", "config")) == {}


def test_create_accepts_catalog_models(store):
    store.create(SwarmConfig("new", "/repo", 2, 1, claude_model="sonnet", codex_model="gpt-6-sol"))
    assert (store.config("new").claude_model, store.config("new").codex_model) == ("sonnet", "gpt-6-sol")


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
    assert (picked.model, picked.effort) == (model, "xhigh" if model == "sonnet" else "medium")
    assert model_pick.frontier(agent, default_model=model).model == model


@pytest.mark.parametrize("maximum", ["low", "medium", "high"])
def test_auto_sonnet_refuses_a_range_below_extra_high(maximum):
    picked = model_pick.ModelPick("sonnet", "xhigh", model_pick.BAND)
    with pytest.raises(runtime.SpawnError, match="range must allow xhigh"):
        runtime._model_args("claude", picked.__dict__, {}, ("low", maximum))


def test_auto_sonnet_launches_at_extra_high_and_keeps_saved_or_explicit_efforts():
    picked = model_pick.ModelPick("sonnet", "xhigh", model_pick.BAND)
    assert runtime._model_args("claude", picked.__dict__, {}, ("medium", "xhigh")) == [
        "--model",
        "sonnet",
        "--effort",
        "xhigh",
    ]
    for source, preserve in ((model_pick.BAND, True), ("lane-default", False)):
        picked = model_pick.ModelPick("sonnet", "high", source)
        assert runtime._model_args("claude", picked.__dict__, {}, ("medium", "high"), preserve) == [
            "--model",
            "sonnet",
            "--effort",
            "high",
        ]


def test_launch_defaults_keep_explicit_choices(store):
    config = replace(store.config("sw"), claude_model="sonnet", codex_model="gpt-6-sol")
    for lane in ("eng", "ci", "plan", "master"):
        picked = runtime._lane_default(lane, "claude", {}, config)
        assert picked.model == "sonnet"
    assert runtime._lane_default("plan", "claude", {"model": "opus"}, config).model == "opus"


@pytest.mark.parametrize("harness, expected", [("claude", "opus"), ("codex", "gpt-6.1-sol")])
def test_missing_config_fields_use_harness_defaults(harness, expected):
    assert default_models.model(None, harness) == expected


def test_default_model_lookup_and_refusal_keep_harnesses_separate(store):
    config = replace(store.config("sw"), claude_model="sonnet", codex_model="gpt-6-sol")
    assert default_models.model(config, "claude") == "sonnet"
    assert default_models.model(config, "codex") == "gpt-6-sol"
    assert default_models.refusal({"claude_model": "sonnet", "codex_model": "gpt-6-sol"}, CATALOG) == ""
    assert default_models.refusal({"max_eng": 3}, {}) == ""
    assert (
        default_models.refusal({"claude_model": "gpt-6-sol"}, CATALOG)
        == "claude_model must be a model listed in the claude catalog"
    )
    assert (
        default_models.refusal({"codex_model": "sonnet"}, CATALOG)
        == "codex_model must be a model listed in the codex catalog"
    )
