"""One supervised Codex app-server per admitted execution: admit, spawn, connect, bind, then open exactly once."""

import os
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from hooks.observability.trace_flush import start_time
from scripts.codex_session import protocol
from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, BindingStore, TransportRecord
from scripts.routing import envs
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore

NO_DAEMON = "--no-daemon"
REQUIRED_HOOKS = ("sessionStart", "userPromptSubmit", "preToolUse", "postToolUse", "stop")
STARTUP_TIMEOUT_S = 30.0
POLL_S = 0.25
STOP_WAIT_S = 5.0
# Linux sun_path holds 108 bytes including the terminator; macOS holds 104.
SOCKET_PATH_MAX = 100


class LaunchFailure(RuntimeError):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class Launch:
    slug: str
    agent: AgentRecord
    cwd: str
    codex_home: str
    environ: Mapping[str, str]
    server_args: tuple = ()
    previous_execution_id: str = ""
    resume_thread: str = ""
    opening: str = ""
    account_type: str = ""
    allowed_tokens: frozenset = frozenset()
    bundle_revision: str = ""
    startup_timeout_s: float = STARTUP_TIMEOUT_S


@dataclass(frozen=True)
class Deps:
    popen: Callable = subprocess.Popen
    connect: Callable = protocol.connect
    start_identity: Callable[[int], int | None] = start_time
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    codex_bin: str = "codex"
    runtime_root: Path | None = None


@dataclass(frozen=True)
class Started:
    record: TransportRecord
    agent: AgentRecord
    process: object


def runtime_root(environ: Mapping[str, str]) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(environ.get("HOME", str(Path.home()))) / ".agentihooks"
    return base / "cx"


def socket_path(root: Path, execution_id: str) -> Path:
    directory = root / execution_id.removeprefix("exe-")[:16]
    path = directory / "s"
    if len(str(path).encode()) > SOCKET_PATH_MAX:
        raise LaunchFailure("endpoint", f"socket path is longer than {SOCKET_PATH_MAX} bytes")
    for private in (root, directory):
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        private.chmod(0o700)
    return path


def server_command(codex_bin: str, path: Path, args: tuple) -> list[str]:
    if NO_DAEMON in args:
        raise LaunchFailure("command", "app-server takes no --no-daemon flag")
    return [codex_bin, "app-server", "--listen", f"unix://{path}", *args]


def owns(record: TransportRecord, start_identity: Callable[[int], int | None]) -> bool:
    if record.server_pid < 1 or record.server_start_identity < 1:
        return False
    return start_identity(record.server_pid) == record.server_start_identity


def check_environment(environ: Mapping[str, str], allowed: frozenset) -> None:
    foreign = sorted(name for name in environ if name.startswith(envs.CODEX_TOKEN_PREFIX) and name not in allowed)
    if foreign:
        raise LaunchFailure("wrong_account", f"child environment carries other account credentials: {foreign}")


def admit(store: RedisStore, launch: Launch, deps: Deps) -> AgentRecord:
    pid_start = deps.start_identity(os.getpid())
    target = {"pid": os.getpid(), "pid_start": pid_start} if pid_start else {}
    agent = replace(launch.agent, harness="codex", transport=APP_SERVER_TRANSPORT, runtime_target=target)
    return store.start_execution(launch.slug, agent, launch.previous_execution_id)


def wait_ready(path: Path, process, record: TransportRecord, launch: Launch, deps: Deps):
    deadline = deps.clock() + launch.startup_timeout_s
    while (left := deadline - deps.clock()) > 0:
        if process.poll() is not None:
            raise LaunchFailure(
                "server_exited", f"app-server exited with status {process.returncode} before it answered"
            )
        if not owns(record, deps.start_identity):
            raise LaunchFailure("server_identity", "the server pid no longer belongs to the started process")
        if path.exists():
            try:
                return deps.connect(str(path), left)
            except (OSError, TimeoutError, protocol.ProtocolError):
                pass
        deps.sleep(POLL_S)
    raise LaunchFailure("startup_timeout", "app-server started but never answered initialize in time")


def ensure_hooks(connection: protocol.Connection, cwd: str) -> None:
    listed = protocol.hooks(connection, cwd)
    missing = [event for event in REQUIRED_HOOKS if event not in {hook.get("eventName") for hook in listed}]
    if missing:
        raise LaunchFailure("hooks_missing", f"required hooks are missing: {missing}")
    untrusted = [hook for hook in listed if hook.get("trustStatus") != "trusted"]
    if untrusted:
        protocol.trust(connection, untrusted)
        still = {
            hook.get("eventName") for hook in protocol.hooks(connection, cwd) if hook.get("trustStatus") != "trusted"
        }
        if still & set(REQUIRED_HOOKS):
            raise LaunchFailure("hooks_untrusted", f"required hooks stay untrusted: {sorted(still)}")


def check_binding(connection: protocol.Connection, info: dict, launch: Launch) -> None:
    if os.path.realpath(info.get("codexHome", "")) != os.path.realpath(launch.codex_home):
        raise LaunchFailure("wrong_profile", "the server runs from another Codex home than the rendered profile")
    ensure_hooks(connection, launch.cwd)
    if launch.account_type and protocol.account_type(connection) != launch.account_type:
        raise LaunchFailure("wrong_account", f"the server is not signed in with a {launch.account_type} account")


def bind_thread(connection: protocol.Connection, launch: Launch) -> dict:
    thread = (
        protocol.resume_thread(connection, launch.resume_thread)
        if launch.resume_thread
        else protocol.start_thread(connection, launch.cwd)
    )
    if not thread.get("id"):
        raise LaunchFailure("protocol", "the server returned no thread id")
    return thread


def stop(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _record(launch: Launch, agent: AgentRecord, path: Path) -> TransportRecord:
    host = socket.gethostname()
    return TransportRecord(
        execution_id=agent.execution_id,
        execution_generation=agent.generation,
        seat=agent.seat,
        agent_name=agent.name,
        task_id=agent.task,
        endpoint_ref=f"unix://{path}",
        host_id=host,
        account=agent.account,
        profile=agent.profile,
        bundle_revision=launch.bundle_revision,
        model=agent.model,
        effort=agent.effort,
        bridge_owner=f"{host}:{os.getpid()}",
    )


def _connect_bound(store, bindings, launch, agent, record, process, deps) -> tuple[TransportRecord, AgentRecord]:
    path = Path(record.endpoint_ref.removeprefix("unix://"))
    connection, info = wait_ready(path, process, record, launch, deps)
    try:
        check_binding(connection, info, launch)
        thread = bind_thread(connection, launch)
        record = replace(
            record,
            thread_id=thread["id"],
            session_id=thread.get("sessionId", ""),
            state=CONNECTED,
            last_transport_activity=deps.now_ms(),
        )
        bindings.put(launch.slug, record)
        agent = replace(agent, conversation_id=thread["id"])
        store.put_agent(launch.slug, agent)
        if launch.opening and bindings.claim_opening(launch.slug, agent.execution_id, deps.now_ms()):
            record = replace(record, active_turn_id=protocol.start_turn(connection, thread["id"], launch.opening))
            bindings.put(launch.slug, record)
        return record, agent
    except (OSError, TimeoutError, protocol.ProtocolError) as exc:
        raise LaunchFailure("protocol", str(exc)) from exc
    finally:
        connection.close()


def launch(store: RedisStore, spec: Launch, deps: Deps) -> Started:
    check_environment(spec.environ, spec.allowed_tokens)
    agent = admit(store, spec, deps)
    bindings = BindingStore(store)
    path = socket_path(deps.runtime_root or runtime_root(spec.environ), agent.execution_id)
    record = bindings.reserve(spec.slug, _record(spec, agent, path))
    process = deps.popen(
        server_command(deps.codex_bin, path, spec.server_args),
        env=dict(spec.environ),
        cwd=spec.cwd,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    record = replace(record, server_pid=process.pid, server_start_identity=deps.start_identity(process.pid) or 0)
    bindings.put(spec.slug, record)
    try:
        record, agent = _connect_bound(store, bindings, spec, agent, record, process, deps)
    except LaunchFailure as failure:
        stop(process)
        bindings.put(spec.slug, replace(record, state=FAILED, last_error_class=failure.error_class))
        bindings.release(spec.slug, record)
        raise
    return Started(record, agent, process)


def finish(store: RedisStore, slug: str, started: Started, now_ms: int) -> TransportRecord:
    bindings = BindingStore(store)
    current = bindings.get(slug, started.record.execution_id) or started.record
    record = replace(current, state=EXITED, active_turn_id="", last_transport_activity=now_ms)
    bindings.put(slug, record)
    bindings.release(slug, record)
    return record
