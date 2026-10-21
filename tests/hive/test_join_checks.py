import json
from types import SimpleNamespace

import pytest


def test_redis_probes_use_disposable_claim_and_verdict_fields():
    from fakeredis import FakeRedis

    from scripts.hive import join_checks
    from scripts.swarm.store import RedisStore

    redis = FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    redis.set(store.key("sw", "claim", "real"), "owner")
    redis.hset(store.key("sw", "findings"), "real", "verdict")
    assert join_checks.redis_checks(redis, "sw", "execution") == {
        "redis_claim": True,
        "redis_release": True,
        "redis_status": True,
        "redis_verdict": True,
    }
    assert redis.get(store.key("sw", "claim", "real")) == "owner"
    assert redis.hgetall(store.key("sw", "findings")) == {"real": "verdict"}
    assert list(redis.scan_iter(match=store.key("sw", "claim", "join-check-execution*"))) == []


def test_cheap_checks_require_rendered_profile_settings_transcript_and_pane(tmp_path, monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import join_checks
    from scripts.swarm import runtime

    home = tmp_path / "home"
    home.mkdir()
    (home / ".agentihooks-render.json").write_text(json.dumps({"chain": ["engineer"], "overlays": []}))
    monkeypatch.setattr(runtime, "herdr_call", lambda args: {"panes": [{"pane_id": "pane"}]})
    monkeypatch.setattr(join_checks, "settings_ready", lambda context: True)
    context = {
        "home": str(home),
        "harness": "claude",
        "profile": "engineer",
        "pane": "pane",
        "transcript_dir": str(tmp_path),
    }
    before = sorted(p.name for p in tmp_path.iterdir())
    report = {"context": context, "slug": "sw", "execution_id": "execution", "started_at": 1000}
    checks = join_checks.cheap(FakeRedis(decode_responses=True), report)
    assert checks["profile"] is True
    assert checks["settings"] is True
    assert checks["transcript"] is True
    assert checks["pane"] is True
    assert checks["started_at"] is True
    assert checks["brain"] is False
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_command_checks_suppress_output_and_fail_on_refusal(monkeypatch):
    from scripts.hive import join_checks

    monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", "pat")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(join_checks.subprocess, "run", run)
    assert join_checks.github_ready() is False
    assert calls[0][0] == ["gh", "api", "user", "--silent"]
    assert calls[0][1]["stdout"] == join_checks.subprocess.DEVNULL
    assert calls[0][1]["stderr"] == join_checks.subprocess.DEVNULL


@pytest.mark.parametrize("exception", [OSError("offline"), TimeoutError("late")])
def test_a_probe_exception_becomes_a_failed_check(exception):
    from scripts.hive import join_checks

    def probe():
        raise exception

    assert join_checks.checked(probe) is False


@pytest.mark.parametrize("mode", [None, "app"])
def test_github_app_credentials_are_checked_as_an_installation(monkeypatch, mode):
    from scripts.hive import join_checks

    if mode is None:
        monkeypatch.delenv("HIVE_GITHUB_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("HIVE_GITHUB_AUTH_MODE", mode)
    calls = []
    monkeypatch.setattr(
        join_checks.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0)
    )
    assert join_checks.github_ready() is True
    assert calls == [["gh", "api", "installation/repositories", "--silent"]]
