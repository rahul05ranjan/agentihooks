import shutil
import stat
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.codex_session import supervisor
from scripts.codex_session.binding import CONNECTED, ENDPOINTS, EXITED, FAILED, BindingStore, decode
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.codex_session.fakes import FIRST_PID, OWN_START, SERVER_START, Clock, Server, hooks

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "fixture"
SEAT = f"eng-1@{SLUG}"


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
    return store


@pytest.fixture
def root():
    path = Path(tempfile.mkdtemp(prefix="cx-"))
    yield path / "cx"
    shutil.rmtree(path)


@pytest.fixture
def server(tmp_path):
    return Server(str(tmp_path / "codex-home"))


@pytest.fixture
def clock():
    return Clock()


def deps(server, clock, root):
    return supervisor.Deps(
        popen=server.popen,
        connect=server.connect,
        start_identity=server.start_identity,
        clock=clock,
        sleep=clock.sleep,
        now_ms=lambda: 1000,
        runtime_root=root,
    )


def launch_spec(store, server, tmp_path, name="", **changes):
    agent = AgentRecord(
        name or store.next_name(SLUG, "eng"), "eng", "task", seat=SEAT, account="default", profile="engineer"
    )
    spec = supervisor.Launch(
        slug=SLUG,
        agent=agent,
        cwd=str(tmp_path),
        codex_home=server.codex_home,
        environ={"CODEX_HOME": server.codex_home, "AGENTIHOOKS_SWARM": SLUG},
        opening="Start the task.",
        account_type="chatgpt",
        startup_timeout_s=2.0,
    )
    return replace(spec, **changes)


def failed(store, server, clock, root, spec, error_class):
    with pytest.raises(supervisor.LaunchFailure) as failure:
        supervisor.launch(store, spec, deps(server, clock, root))
    assert failure.value.error_class == error_class
    return failure.value


def only_record(store):
    rows = store.redis.hvals(store.key(SLUG, "codex-transport"))
    assert len(rows) == 1
    return decode(rows[0])


def test_a_launch_admits_one_server_binds_its_thread_and_opens_once(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    record, agent = started.record, started.agent
    path = Path(record.endpoint_ref.removeprefix("unix://"))
    assert server.commands == [["codex", "app-server", "--listen", f"unix://{path}"]]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert (record.state, record.thread_id, record.session_id) == (CONNECTED, "thread-new", "session-new")
    assert (record.server_pid, record.server_start_identity) == (FIRST_PID, SERVER_START)
    assert record.active_turn_id == "turn-opening"
    assert (record.execution_id, record.execution_generation, record.seat) == (agent.execution_id, 1, SEAT)
    admitted = store.execution(SLUG, agent.execution_id)
    assert (admitted.transport, admitted.harness, admitted.conversation_id) == (
        APP_SERVER_TRANSPORT,
        "codex",
        "thread-new",
    )
    assert admitted.runtime_target["pid_start"] == OWN_START
    assert server.methods().count("turn/start") == 1
    assert server.methods().index("thread/start") < server.methods().index("turn/start")
    assert not BindingStore(store).claim_opening(SLUG, agent.execution_id, 2000)
    assert all(connection.closed for connection in server.connections)


def test_a_duplicate_launch_for_an_occupied_seat_spawns_nothing(store, server, clock, root, tmp_path):
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    with pytest.raises(SwarmError, match="replacement does not match the current execution"):
        supervisor.launch(store, launch_spec(store, server, tmp_path, first.agent.name), deps(server, clock, root))
    assert len(server.commands) == 1
    assert [agent.execution_id for agent in store.executions(SLUG, SEAT)] == [first.agent.execution_id]


def test_a_stale_predecessor_is_refused_before_any_spawn(store, server, clock, root, tmp_path):
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    stale = launch_spec(store, server, tmp_path, first.agent.name, previous_execution_id="exe-stale")
    with pytest.raises(SwarmError, match="replacement does not match the current execution"):
        supervisor.launch(store, stale, deps(server, clock, root))
    assert len(server.commands) == 1
    current = launch_spec(store, server, tmp_path, first.agent.name, previous_execution_id=first.agent.execution_id)
    second = supervisor.launch(store, current, deps(server, clock, root))
    assert second.agent.generation == 2
    assert second.record.endpoint_ref != first.record.endpoint_ref


def test_a_resume_binds_the_existing_thread_without_starting_one(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path, resume_thread="thread-kept")
    started = supervisor.launch(store, spec, deps(server, clock, root))
    assert started.record.thread_id == "thread-kept"
    assert "thread/start" not in server.methods()
    assert ("thread/resume", {"threadId": "thread-kept"}) in server.requests


def assert_failed_cleanly(store, server, error_class):
    record = only_record(store)
    assert (record.state, record.last_error_class) == (FAILED, error_class)
    assert record.thread_id == ""
    assert server.processes[-1].poll() is not None
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}
    assert "turn/start" not in server.methods()


def test_a_server_running_from_another_codex_home_is_refused(store, server, clock, root, tmp_path):
    server.reported_home = str(tmp_path / "other-profile")
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "wrong_profile")
    assert_failed_cleanly(store, server, "wrong_profile")


def test_a_server_signed_in_with_another_account_kind_is_refused(store, server, clock, root, tmp_path):
    server.account = "apiKey"
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "wrong_account")
    assert_failed_cleanly(store, server, "wrong_account")


def test_other_account_credentials_in_the_child_env_refuse_before_admission(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path)
    spec = replace(
        spec, environ={**spec.environ, "AH_CX_TOKEN_other": "unused"}, allowed_tokens=frozenset({"AH_CX_TOKEN_mine"})
    )
    failure = failed(store, server, clock, root, spec, "wrong_account")
    assert "AH_CX_TOKEN_other" in str(failure)
    assert server.commands == []
    assert store.executions(SLUG, SEAT) == []


def test_the_selected_account_credential_is_allowed(store, server, clock, root, tmp_path):
    spec = launch_spec(store, server, tmp_path)
    spec = replace(
        spec, environ={**spec.environ, "AH_CX_TOKEN_mine": "unused"}, allowed_tokens=frozenset({"AH_CX_TOKEN_mine"})
    )
    supervisor.launch(store, spec, deps(server, clock, root))
    assert server.envs[0]["AH_CX_TOKEN_mine"] == "unused"


def test_missing_required_hooks_are_refused(store, server, clock, root, tmp_path):
    server.hooks = hooks(events=("sessionStart", "userPromptSubmit", "preToolUse", "postToolUse"))
    failure = failed(store, server, clock, root, launch_spec(store, server, tmp_path), "hooks_missing")
    assert "stop" in str(failure)
    assert_failed_cleanly(store, server, "hooks_missing")


def test_untrusted_hooks_are_trusted_before_the_thread_starts(store, server, clock, root, tmp_path):
    server.hooks = hooks(trust="untrusted")
    supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    methods = server.methods()
    assert methods.index("config/batchWrite") < methods.index("thread/start")
    edits = dict(server.requests)["config/batchWrite"]["edits"]
    assert edits[0] == {
        "keyPath": 'hooks.state."k:sessionStart".trusted_hash',
        "value": "h",
        "mergeStrategy": "replace",
    }


def test_hooks_that_stay_untrusted_are_refused(store, server, clock, root, tmp_path):
    server.hooks = hooks(trust="untrusted")
    server.trust_to = "untrusted"
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "hooks_untrusted")
    assert_failed_cleanly(store, server, "hooks_untrusted")


def test_a_server_that_came_up_but_never_answered_times_out_and_is_stopped(store, server, clock, root, tmp_path):
    server.answers = False
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "startup_timeout")
    record = only_record(store)
    assert (record.server_pid, record.server_start_identity) == (FIRST_PID, SERVER_START)
    assert Path(record.endpoint_ref.removeprefix("unix://")).exists()
    assert server.processes[0].stopped
    assert clock.now >= 2.0
    assert_failed_cleanly(store, server, "startup_timeout")


def test_a_server_that_exits_before_it_answers_fails_without_waiting(store, server, clock, root, tmp_path):
    server.exits = True
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "server_exited")
    assert clock.now == 0.0
    assert only_record(store).state == FAILED


def test_a_missing_codex_binary_fails_the_launch_and_frees_the_endpoint(store, server, clock, root, tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    server.popen = missing
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "spawn")
    record = only_record(store)
    assert (record.state, record.last_error_class, record.server_pid) == (FAILED, "spawn", 0)
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}


def test_any_error_after_the_spawn_stops_the_server(store, server, clock, root, tmp_path, monkeypatch):
    def refused(slug, agent):
        raise SwarmError("agent registry kept changing; update was not committed")

    monkeypatch.setattr(store, "put_agent", refused)
    with pytest.raises(SwarmError, match="agent registry kept changing"):
        supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert server.processes[0].stopped
    record = only_record(store)
    assert (record.state, record.last_error_class) == (FAILED, "SwarmError")
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}


def test_a_reused_pid_is_not_mistaken_for_the_server(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    assert supervisor.owns(started.record, server.start_identity)
    server.starts[started.record.server_pid] = SERVER_START + 1
    assert not supervisor.owns(started.record, server.start_identity)
    server.starts.pop(started.record.server_pid)
    assert not supervisor.owns(replace(started.record, server_start_identity=0), server.start_identity)
    assert not supervisor.owns(replace(started.record, server_pid=0), server.start_identity)


def test_a_pid_taken_over_during_startup_fails_the_launch(store, server, clock, root, tmp_path):
    original = server.start_identity
    server.start_identity = lambda pid: SERVER_START + 1 if clock.now > 0 else original(pid)
    server.answers = False
    failed(store, server, clock, root, launch_spec(store, server, tmp_path), "server_identity")


def test_two_executions_get_two_private_endpoints(store, server, clock, root, tmp_path):
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    other = launch_spec(store, server, tmp_path)
    other = replace(other, agent=replace(other.agent, seat=f"eng-2@{SLUG}"))
    second = supervisor.launch(store, other, deps(server, clock, root))
    assert first.record.endpoint_ref != second.record.endpoint_ref
    assert len(store.redis.hgetall(store.key(SLUG, ENDPOINTS))) == 2


def test_the_server_command_never_carries_no_daemon(root):
    with pytest.raises(supervisor.LaunchFailure) as failure:
        supervisor.server_command("codex", root / "s", ("-c", "x=1", "--no-daemon"))
    assert failure.value.error_class == "command"
    assert supervisor.server_command("codex", root / "s", ("-c", "x=1")) == [
        "codex",
        "app-server",
        "--listen",
        f"unix://{root / 's'}",
        "-c",
        "x=1",
    ]


def test_a_socket_path_past_the_limit_is_refused(tmp_path):
    with pytest.raises(supervisor.LaunchFailure) as failure:
        supervisor.socket_path(tmp_path / ("d" * supervisor.SOCKET_PATH_MAX), "exe-0123456789abcdef")
    assert failure.value.error_class == "endpoint"


def test_the_runtime_root_prefers_the_user_runtime_dir():
    assert supervisor.runtime_root({"XDG_RUNTIME_DIR": "/run/user/7", "HOME": "/home/u"}) == Path("/run/user/7/cx")
    assert supervisor.runtime_root({"HOME": "/home/u"}) == Path("/home/u/.agentihooks/cx")


def test_finish_marks_the_record_exited_and_frees_the_endpoint(store, server, clock, root, tmp_path):
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, clock, root))
    record = supervisor.finish(store, SLUG, started, 5000)
    assert (record.state, record.active_turn_id, record.last_transport_activity) == (EXITED, "", 5000)
    assert BindingStore(store).get(SLUG, started.record.execution_id) == record
    assert store.redis.hgetall(store.key(SLUG, ENDPOINTS)) == {}
