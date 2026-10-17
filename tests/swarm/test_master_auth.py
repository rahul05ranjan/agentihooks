from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("five_left,week_left", [(90, 25), (90, 9), (4, 25)])
def test_only_the_master_can_use_reserved_room(tmp_path, lane, five_left, week_left):
    from scripts.swarm.capacity import Account
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import SpawnError
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        out = "status=started\nroute_status=routed\naccount=reserved\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [
        Account("claude", "reserved", "CLOSED", 0, five_left, week_left, 0, reserved="worker ceiling")
    ]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": lane, "title": "proof", "route_kind": "subscription"}
    if lane != "master" or five_left == 4 or week_left == 9:
        with pytest.raises(SpawnError):
            runtime.spawn(config, lane, "agent", task)
        assert not seen
        return
    placed = runtime.spawn(config, lane, "agent", task)
    passed = seen[0][seen[0].index("--") + 1 :]
    assert passed[:2] == ["--route", "reserved"]
    assert placed.account == "reserved"
    assert placed.route_kind == "subscription"
    assert runtime._quota_accounts[0].sessions == 1
    assert runtime._quota_accounts[0].cap == 0


@pytest.mark.parametrize("login", [True, False])
@pytest.mark.parametrize("handoff", [True, False])
def test_master_prefers_a_valid_login_even_after_a_token_handoff(tmp_path, login, handoff):
    from scripts.routing import native_login
    from scripts.swarm.capacity import Account
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=0 if login else 1, stdout=native_login.STATUS if login else "", stderr="")
        seen.append(argv)
        out = "status=started\nroute_status=routed\naccount=token\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [Account("claude", "token", "OPEN", 0, 90, 90, 6)]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof"}
    if handoff:
        task["handoff"] = "Continue the master task."
        task["handoff_envelope"] = {
            "launch": {"profile": "master", "harness": "claude", "account": "token", "model": "opus", "effort": "high"}
        }
    placed = runtime.spawn(config, "master", "agent", task)
    passed = seen[0][seen[0].index("--") + 1 :]
    assert passed[:2] == ["--route", "interactive" if login else "token"]
    assert placed.route_kind == ("interactive" if login else "subscription")
