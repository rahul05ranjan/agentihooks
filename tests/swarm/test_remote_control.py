import json
import shlex
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import init_agent
from scripts.swarm import remote_control
from scripts.swarm.health import findings
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import Placed, placed_record
from tests.swarm.test_runtime import _passed, _resuming, validated

RC = ["--remote-control", "master · proof", "--remote-control-session-name-prefix", "hive-a"]
ON = {"state": "on", "detail": "master · proof"}


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
    placed = runtime.spawn(config, lane, "agent", {"id": lane, "title": "proof", "route_kind": kind})
    return _passed(seen[0]), placed


def _rc(passed):
    if "--remote-control" not in passed:
        return []
    at = passed.index("--remote-control")
    return passed[at : at + 4]


def test_an_interactive_claude_master_spawns_with_remote_control_after_its_route(tmp_path):
    passed, placed = _spawn(tmp_path, "master", "claude", "interactive")
    assert passed[:6] == ["--route", "interactive", *RC]
    assert placed.remote_control == ON


@pytest.mark.parametrize(
    ("kind", "decided"),
    [("subscription", {"state": "off", "detail": "fallback auth"}), ("api", {"state": "off", "detail": "gateway"})],
)
def test_token_and_api_masters_spawn_without_remote_control_and_record_why(tmp_path, kind, decided):
    passed, placed = _spawn(tmp_path, "master", "claude", kind)
    assert _rc(passed) == []
    assert placed.remote_control == decided


@pytest.mark.parametrize(("lane", "harness"), [("master", "codex"), ("eng", "claude")])
def test_codex_masters_and_engineers_spawn_without_remote_control_or_a_record(tmp_path, lane, harness):
    passed, placed = _spawn(tmp_path, lane, harness, "interactive")
    assert _rc(passed) == []
    assert placed.remote_control == {}


def test_the_flag_turns_remote_control_off_at_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv(remote_control.FLAG, "false")
    passed, placed = _spawn(tmp_path, "master", "claude", "interactive")
    assert _rc(passed) == []
    assert placed.remote_control == {"state": "off", "detail": "flag"}


def test_a_master_placed_on_the_api_route_records_gateway(tmp_path):
    def run(argv, **kwargs):
        output = "status=started\nroute_status=routed\naccount=api\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, output), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "master", "agent", {"id": "master", "title": "proof"})
    assert (placed.route_kind, placed.remote_control) == ("api", {"state": "off", "detail": "gateway"})


def _resumed(tmp_path, lane, harness, kind):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness)
    config.slug = "proof"
    placed = runtime.resume(config, replace(agent, lane=lane, task=lane, route_kind=kind), "resume")
    return _passed(seen["runs"][0]), placed


def test_an_interactive_claude_master_resumes_with_remote_control_after_its_route(tmp_path):
    passed, placed = _resumed(tmp_path, "master", "claude", "interactive")
    assert passed[:6] == ["--route", "interactive", *RC]
    assert placed.remote_control == ON


@pytest.mark.parametrize(
    ("lane", "harness", "kind", "decided"),
    [
        ("master", "claude", "subscription", {"state": "off", "detail": "fallback auth"}),
        ("master", "codex", "interactive", {}),
        ("eng", "claude", "interactive", {}),
    ],
)
def test_token_codex_and_engineer_resumes_carry_no_remote_control(tmp_path, lane, harness, kind, decided):
    passed, placed = _resumed(tmp_path, lane, harness, kind)
    assert _rc(passed) == []
    assert placed.remote_control == decided


def test_the_flag_turns_remote_control_off_at_resume(tmp_path, monkeypatch):
    monkeypatch.setenv(remote_control.FLAG, "off")
    passed, placed = _resumed(tmp_path, "master", "claude", "interactive")
    assert _rc(passed) == []
    assert placed.remote_control == {"state": "off", "detail": "flag"}


def test_an_on_decision_names_the_master_seat_role_and_the_slug():
    assert remote_control.decision("master", "claude", "interactive", "rig", {}) == {
        "state": "on",
        "detail": "master · rig",
    }


def test_an_unset_or_other_flag_value_keeps_remote_control_on():
    assert remote_control.off_reason("interactive", {}) == ""
    assert remote_control.off_reason("interactive", {remote_control.FLAG: "none"}) == ""


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off"])
def test_each_off_value_of_the_flag_reports_flag(value):
    assert remote_control.off_reason("interactive", {remote_control.FLAG: value}) == "flag"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("interactive", ""), ("api", "gateway"), ("subscription", "fallback auth"), ("", "fallback auth")],
)
def test_off_reason_by_route_kind(kind, reason):
    assert remote_control.off_reason(kind, {remote_control.FLAG: "on"}) == reason


def test_only_an_on_decision_produces_launch_arguments():
    assert remote_control.arguments(ON) == RC
    assert remote_control.arguments({"state": "off", "detail": "flag"}) == []
    assert remote_control.arguments({}) == []


@pytest.mark.parametrize(
    ("decided", "text"),
    [
        (ON, "Remote Control: on (master · proof)"),
        ({"state": "off", "detail": "fallback auth"}, "Remote Control: off (fallback auth)"),
        ({}, ""),
        (None, ""),
    ],
)
def test_status_line_reads_the_recorded_decision(decided, text):
    assert remote_control.line({"remote_control": decided}) == text


def test_placement_records_the_decision_and_keeps_it_when_a_relaunch_has_none():
    agent = AgentRecord("master@a1b2c3-0001", "master", "master")
    placed = placed_record(agent, Placed("w1:p1", "claude", remote_control=ON))
    assert placed.remote_control == ON
    assert placed_record(placed, Placed("w1:p2", "claude")).remote_control == ON


@pytest.mark.parametrize(
    ("message", "failed"),
    [
        ("Remote Control failed · /remote-control", True),
        ("Remote Control disconnected — run /login to restore Remote Control", True),
        ("Remote Control stopped: the server refused to reconnect this computer", True),
        ("Remote Control session expired", True),
        ("Remote Control back on", False),
        ("Upload failed", False),
        ("Remote Control on. Upload failed", False),
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


def test_a_skipped_row_does_not_hide_a_later_master_failure():
    rows = [
        {**_master(remote_control_failure={"message": "m", "at": 5_000}), "lane": "eng", "name": "e"},
        _master(remote_control_failure={"message": "Remote Control failed", "at": 2_000}),
    ]
    assert [f.id for f in findings.master_rc(rows)] == ["master-rc/master@proof"]


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
    out = capsys.readouterr().out
    assert rc == 0 and "trust=marked" in out
    assert f"--remote-control {shlex.quote('master · proof')}" in out
    assert json.loads((tmp_path / ".claude.json").read_text())["projects"][str(project)]["hasTrustDialogAccepted"]
