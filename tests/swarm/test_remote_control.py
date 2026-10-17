import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import init_agent
from scripts.swarm import remote_control
from scripts.swarm.health import findings
from scripts.swarm.runtime import HerdrRuntime
from tests.swarm.test_runtime import _passed, _resuming, validated

RC = ["--remote-control", "master · proof", "--remote-control-session-name-prefix", "hive-a"]


@pytest.fixture(autouse=True)
def hive(monkeypatch):
    from scripts.routing import native_login

    monkeypatch.setenv("SWARM_HIVE_ID", "hive-a")
    monkeypatch.delenv(remote_control.FLAG, raising=False)
    monkeypatch.setattr(native_login, "check", lambda *_: {"loggedIn": True, "authMethod": "claude.ai"})


def _spawn(tmp_path, lane, harness, kind):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": harness}},
        autonomy="delegate",
    )
    runtime.spawn(config, lane, "agent", {"id": lane, "title": "proof", "route_kind": kind})
    return _passed(seen[0])


def _rc(passed):
    if "--remote-control" not in passed:
        return []
    at = passed.index("--remote-control")
    return passed[at : at + 4]


def test_an_interactive_claude_master_spawns_with_remote_control_after_its_route(tmp_path):
    passed = _spawn(tmp_path, "master", "claude", "interactive")
    assert passed[:6] == ["--route", "interactive", *RC]


@pytest.mark.parametrize(
    ("lane", "harness", "kind"),
    [("master", "claude", "subscription"), ("master", "claude", "api"), ("master", "codex", "interactive")],
)
def test_token_api_and_codex_masters_spawn_without_remote_control(tmp_path, lane, harness, kind):
    assert _rc(_spawn(tmp_path, lane, harness, kind)) == []


def test_an_engineer_on_the_interactive_route_spawns_without_remote_control(tmp_path):
    assert _rc(_spawn(tmp_path, "eng", "claude", "interactive")) == []


def test_the_flag_turns_remote_control_off_at_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv(remote_control.FLAG, "false")
    assert _rc(_spawn(tmp_path, "master", "claude", "interactive")) == []


def _resumed(tmp_path, lane, harness, kind):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness)
    config.slug = "proof"
    runtime.resume(config, replace(agent, lane=lane, task=lane, route_kind=kind), "resume")
    return _passed(seen["runs"][0])


def test_an_interactive_claude_master_resumes_with_remote_control_after_its_route(tmp_path):
    passed = _resumed(tmp_path, "master", "claude", "interactive")
    assert passed[:6] == ["--route", "interactive", *RC]


@pytest.mark.parametrize(
    ("lane", "harness", "kind"),
    [
        ("master", "claude", "subscription"),
        ("master", "codex", "interactive"),
        ("eng", "claude", "interactive"),
    ],
)
def test_token_codex_and_engineer_resumes_carry_no_remote_control(tmp_path, lane, harness, kind):
    assert _rc(_resumed(tmp_path, lane, harness, kind)) == []


def test_the_session_name_uses_the_seat_role_and_the_slug():
    assert remote_control.session_name("master@proof", "proof") == "master · proof"


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off"])
def test_each_off_value_of_the_flag_reports_flag(value):
    assert remote_control.off_reason("interactive", {remote_control.FLAG: value}) == "flag"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("interactive", ""), ("api", "gateway"), ("subscription", "fallback auth"), ("", "fallback auth")],
)
def test_off_reason_by_route_kind(kind, reason):
    assert remote_control.off_reason(kind, {remote_control.FLAG: "on"}) == reason


@pytest.mark.parametrize(
    ("agent", "environ", "text"),
    [
        (
            {"lane": "master", "harness": "claude", "route_kind": "interactive", "seat": "master@proof"},
            {},
            "Remote Control: on (master · proof)",
        ),
        (
            {"lane": "master", "harness": "claude", "route_kind": "interactive"},
            {},
            "Remote Control: on (master · proof)",
        ),
        (
            {"lane": "master", "harness": "claude", "route_kind": "subscription"},
            {},
            "Remote Control: off (fallback auth)",
        ),
        ({"lane": "master", "harness": "claude", "route_kind": "api"}, {}, "Remote Control: off (gateway)"),
        (
            {"lane": "master", "harness": "claude", "route_kind": "interactive"},
            {remote_control.FLAG: "off"},
            "Remote Control: off (flag)",
        ),
        ({"lane": "master", "harness": "codex", "route_kind": "interactive"}, {}, ""),
        ({"lane": "eng", "harness": "claude", "route_kind": "interactive"}, {}, ""),
    ],
)
def test_status_line_for_each_master_state(agent, environ, text):
    assert remote_control.line(agent, "proof", environ) == text


@pytest.mark.parametrize(
    ("message", "failed"),
    [
        ("Remote Control failed · /remote-control", True),
        ("Remote Control disconnected — run /login to restore Remote Control", True),
        ("Claude needs your permission to use Bash", False),
        ("", False),
    ],
)
def test_only_remote_control_failure_notices_count(message, failed):
    assert remote_control.failure(message) is failed


def _master(**extra):
    return {"name": "master@a1b2c3-0001", "lane": "master", "seat": "master@proof", "started_at": 1_000, **extra}


def test_a_failure_since_the_master_started_is_a_master_rc_finding():
    rows = [_master(remote_control_failure={"message": "Remote Control failed", "at": 1_000})]
    found = findings.master_rc(rows)
    assert [f.id for f in found] == ["master-rc/master@proof"]
    assert found[0].summary == "Remote Control failed on the master, phone access lost"
    assert found[0].evidence == ("notice: Remote Control failed", "agent master@a1b2c3-0001")
    assert found[0].threshold == "a Remote Control failure notice since the master started"
    assert found[0].measure == 1


@pytest.mark.parametrize(
    "row",
    [
        _master(remote_control_failure={"message": "Remote Control failed", "at": 999}),
        _master(remote_control_failure=None),
        {**_master(remote_control_failure={"message": "Remote Control failed", "at": 5_000}), "lane": "eng"},
    ],
    ids=["before-start", "none", "not-master"],
)
def test_old_missing_or_worker_failures_raise_no_finding(row):
    assert findings.master_rc([row]) == []


def test_a_master_without_a_seat_or_start_is_named_by_agent():
    row = {"name": "master@a1b2c3-0001", "lane": "master", "remote_control_failure": {"message": "m", "at": 0}}
    assert [f.id for f in findings.master_rc([row])] == ["master-rc/master@a1b2c3-0001"]


def test_findings_include_the_master_rc_detector():
    ledger = {"tasks": [], "_meta": {"events": []}}
    rows = [_master(remote_control_failure={"message": "Remote Control failed", "at": 2_000})]
    found = findings.findings(ledger, rows, {}, findings.Limits())
    assert "master-rc/master@proof" in [f.id for f in found]


def test_a_master_launch_with_remote_control_still_marks_its_folder_trusted(monkeypatch, tmp_path, capsys):
    project = tmp_path / "fresh"
    project.mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {}}))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(
        init_agent.subprocess, "Popen", lambda command, **kwargs: Path(command[-1]).with_suffix(".started").touch()
    )
    argv = ["--dir", str(project), "--name", "master", "--agent", "claude", "--start-timeout", "0"]
    rc = init_agent.main(
        [*argv, "--route-timeout", "0", "--", "--route", "interactive", *RC],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
    )
    assert rc == 0 and "trust=marked" in capsys.readouterr().out
    assert json.loads((tmp_path / ".claude.json").read_text())["projects"][str(project)]["hasTrustDialogAccepted"]
