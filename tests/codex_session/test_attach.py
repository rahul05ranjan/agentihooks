import shlex
import sys
from dataclasses import replace

import pytest

from scripts import init_agent
from scripts.codex_session import attach
from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, STARTING, TransportRecord

pytestmark = pytest.mark.unit

RECORD = TransportRecord(
    execution_id="exe-1",
    execution_generation=1,
    seat="master@sw",
    agent_name="sw-master-1",
    task_id="master",
    endpoint_ref="unix:///run/exe-1/codex.sock",
    thread_id="thread-1",
    state=CONNECTED,
)


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def reads(*records):
    rows = list(records)
    return lambda: rows.pop(0) if len(rows) > 1 else rows[0]


def test_the_viewer_resumes_the_recorded_thread_over_the_recorded_endpoint():
    assert attach.viewer_argv("codex", RECORD) == [
        "codex",
        "resume",
        "--remote",
        "unix:///run/exe-1/codex.sock",
        "thread-1",
    ]


@pytest.mark.parametrize(
    ("record", "ready"),
    [
        (RECORD, True),
        (None, False),
        (replace(RECORD, state=STARTING), False),
        (replace(RECORD, thread_id=""), False),
    ],
)
def test_only_a_connected_execution_with_a_thread_is_attachable(record, ready):
    assert attach.attachable(record) is ready


def test_the_viewer_waits_until_the_thread_is_connected():
    clock = Clock()
    found = attach.wait_attachable(reads(None, replace(RECORD, state=STARTING), RECORD), 10, clock, clock.sleep)
    assert found == RECORD
    assert clock.slept == [0.5, 0.5]


@pytest.mark.parametrize("state", [FAILED, EXITED])
def test_an_ended_execution_refuses_the_viewer(state):
    clock = Clock()
    with pytest.raises(attach.AttachError) as refused:
        attach.wait_attachable(reads(replace(RECORD, state=state)), 10, clock, clock.sleep)
    assert str(refused.value) == f"the execution's app-server {state}; there is no thread to attach"
    assert clock.slept == []


def test_a_thread_that_never_connects_times_out():
    clock = Clock()
    with pytest.raises(attach.AttachError) as refused:
        attach.wait_attachable(reads(None), 1.5, clock, clock.sleep)
    assert str(refused.value) == "the execution has no connected thread after 1.5 seconds"
    assert clock.now == 1.5


def test_a_viewer_that_ran_and_closed_returns_without_another_attach():
    clock, ran = Clock(), []

    def run(command):
        ran.append(command)
        clock.now += 5.0
        return 1

    assert attach.attach(reads(RECORD), run, "codex", 10, clock, clock.sleep) == 1
    assert ran == [attach.viewer_argv("codex", RECORD)]


def test_a_closed_viewer_with_a_clean_exit_returns_at_once():
    clock, ran = Clock(), []
    assert attach.attach(reads(RECORD), lambda command: ran.append(command) or 0, "codex", 10, clock, clock.sleep) == 0
    assert len(ran) == 1 and clock.slept == []


def test_a_viewer_refused_before_the_first_rollout_is_retried_a_bounded_number_of_times():
    clock, ran = Clock(), []
    assert attach.attach(reads(RECORD), lambda command: ran.append(command) or 2, "codex", 10, clock, clock.sleep) == 2
    assert len(ran) == 3
    assert clock.slept == [0.5] * 3


def test_a_viewer_that_attaches_on_its_second_try_keeps_running():
    clock, codes = Clock(), [2, 0]
    assert attach.attach(reads(RECORD), lambda command: codes.pop(0), "codex", 10, clock, clock.sleep) == 0
    assert codes == []


def test_main_attaches_the_named_agent_with_the_given_binary():
    ran = []
    assert (
        attach.main(
            ["--slug", "sw", "--agent", "sw-master-1", "--codex-bin", "/bin/codex"],
            reads(RECORD),
            lambda c: ran.append(c) or 0,
        )
        == 0
    )
    assert ran == [attach.viewer_argv("/bin/codex", RECORD)]


def test_main_reports_a_refused_attach(capsys):
    code = attach.main(["--slug", "sw", "--agent", "a", "--timeout", "0"], reads(None), lambda c: 0)
    assert code == 3
    assert (
        capsys.readouterr().err == "agentihooks codex-session: the execution has no connected thread after 0 seconds\n"
    )


def test_the_parser_defaults():
    args = attach._parser().parse_args(["--slug", "sw", "--agent", "a"])
    assert (args.codex_bin, args.timeout) == ("codex", 120.0)


def test_recorded_reads_the_named_agents_current_transport(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord

    agents = [
        AgentRecord("other", "eng", "t1", harness="codex", transport=APP_SERVER_TRANSPORT),
        AgentRecord("sw-master-1", "master", "master", harness="codex", transport=APP_SERVER_TRANSPORT),
        AgentRecord("legacy", "eng", "t2", harness="codex"),
    ]
    store = SimpleNamespace(agents=lambda slug: agents if slug == "sw" else [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
    monkeypatch.setattr(
        "scripts.swarm.live_binding.transport",
        lambda found, agent: RECORD if found is store and agent.name == "sw-master-1" else None,
    )
    assert attach.recorded("sw", "sw-master-1")() == RECORD
    assert attach.recorded("sw", "missing")() is None
    assert attach.recorded("other", "sw-master-1")() is None
    with pytest.raises(attach.AttachError) as refused:
        attach.recorded("sw", "legacy")()
    assert str(refused.value) == "legacy runs no app-server a native terminal can attach to"


def test_the_app_server_pane_runs_the_server_in_its_own_session_and_the_viewer_in_front(tmp_path):
    spec = init_agent.AgentSpec(agent="codex", profile="engineer", transport=init_agent.APP_SERVER, seat="master@sw")
    launcher, _ = init_agent._write_launcher(
        tmp_path, "sw-master-1", "Open.", [], {"XDG_RUNTIME_DIR": str(tmp_path), "AGENTIHOOKS_SWARM": "sw"}, spec
    )
    lines = launcher.read_text().splitlines()
    serve = next(index for index, line in enumerate(lines) if line.startswith("setsid -f "))
    words = shlex.split(lines[serve])
    log = launcher.with_name(f"{launcher.stem}.serve.log")
    assert words[2:6] == [sys.executable, "-m", "scripts.codex_session", "serve"]
    assert words[-3:] == ["</dev/null", f">>{log}", "2>&1"]
    assert shlex.split(lines[serve + 1]) == [
        sys.executable,
        "-m",
        "scripts.codex_session",
        "attach",
        "--slug",
        "sw",
        "--agent",
        "sw-master-1",
    ]
    assert str(log) not in lines[serve + 2]
