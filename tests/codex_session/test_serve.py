from types import SimpleNamespace

import pytest

from scripts import codex_router
from scripts.codex_session import serve, supervisor
from scripts.routing import codex_api

pytestmark = pytest.mark.unit

SWARM_ENV = {
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_AGENT_NAME": "engineer@abc-0001",
}


def test_take_route_reads_both_spellings_and_keeps_the_rest():
    assert serve.take_route(["-m", "m", "--route", "acct", "-c", "x=1"]) == ("acct", ["-m", "m", "-c", "x=1"])
    assert serve.take_route(["--route=interactive"]) == ("interactive", [])
    assert serve.take_route(["--route"]) == ("", ["--route"])


def test_server_options_turn_the_model_into_config_and_keep_config_flags():
    model, effort, args = serve.server_options(
        ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "--enable", "hooks", "-c", "x=1"]
    )
    assert (model, effort) == ("gpt-6.1-sol", "high")
    assert args == (
        "-c",
        'model="gpt-6.1-sol"',
        "-c",
        'model_reasoning_effort="high"',
        "--enable",
        "hooks",
        "-c",
        "x=1",
    )
    assert serve.server_options(["--model", "m"]) == ("m", "", ("-c", 'model="m"'))
    assert serve.server_options([]) == ("", "", ())


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["-s", "read-only"], "app-server takes only -m, -c, --enable and --disable; got -s"),
        (["--no-daemon", "x"], "got --no-daemon"),
        (["-c"], "-c requires a value"),
    ],
)
def test_server_options_refuse_flags_the_app_server_lacks(flags, reason):
    with pytest.raises(ValueError, match=reason):
        serve.server_options(flags)


def test_provider_args_follow_the_routed_account():
    api = codex_router.CodexAccount("api", key_env="OPENAI_API_KEY", base_url="https://api.example.test/v1")
    assert serve.provider_args(api) == tuple(codex_api.overrides("OPENAI_API_KEY", "https://api.example.test/v1"))
    assert serve.provider_args(codex_router.CodexAccount("t", "AH_CX_TOKEN_t", bearer=True)) == tuple(
        codex_router.bearer_overrides()
    )
    assert serve.provider_args(codex_router.CodexAccount("default")) == ()


def test_account_type_is_checked_only_for_default_interactive_and_api_accounts():
    assert serve.account_type(codex_router.CodexAccount("api", key_env="OPENAI_API_KEY"), "") == "apiKey"
    assert serve.account_type(codex_router.CodexAccount("default"), "") == "chatgpt"
    assert serve.account_type(codex_router.CodexAccount("me"), serve.INTERACTIVE) == "chatgpt"
    assert serve.account_type(codex_router.CodexAccount("t", "AH_CX_TOKEN_t"), "") == ""
    assert serve.account_type(codex_router.CodexAccount("b", "AH_CX_TOKEN_b", bearer=True), "") == ""


def test_serve_refuses_outside_a_swarm_execution_and_reports_the_failure(tmp_path, capsys):
    report = tmp_path / "route"
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw", "--report", str(report)]
    assert serve.main(argv, {"HOME": str(tmp_path)}) == 2
    assert "runs only inside a swarm execution" in capsys.readouterr().err
    assert report.read_text().startswith("status=failed\n")


def test_prepare_routes_renders_and_builds_the_launch(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    prompt = tmp_path / "opening"
    prompt.write_text("Begin.", encoding="utf-8")
    calls = {}

    def prepare(name, agent, model, effort, args, environ, overlays):
        calls["prepare"] = (name, agent, args, list(overlays), environ["AGENTIHOOKS_BUNDLE_REVISION"])
        return {"CODEX_HOME": str(home)}, ["-m", "gpt-6.1-sol", *args]

    def route(environ, route, exclude):
        calls["route"] = (route, exclude, environ["CODEX_HOME"])
        return codex_router.CodexAccount("t", "AH_CX_TOKEN_t"), "open", {"CODEX_HOME": str(home), "AH_CX_TOKEN_t": "x"}

    monkeypatch.setattr(serve.select_profile, "prepare", prepare)
    monkeypatch.setattr(serve, "route_account", route)
    argv = [
        "serve",
        "--cwd",
        str(tmp_path),
        "--profile",
        "engineer",
        "--seat",
        "eng-1@sw",
        "--overlay=frontend",
        "--bundle-revision=abc",
        "--exclude",
        "old",
        "--report",
        str(tmp_path / "route"),
        "--previous-execution",
        "exe-prev",
        "--resume",
        "thread-kept",
        "--prompt-file",
        str(prompt),
    ]
    args = serve._parser().parse_args(argv)
    spec = serve.prepare(args, ["--route", "t", "-c", "x=1"], dict(SWARM_ENV))
    assert calls["prepare"] == ("engineer", "codex", ["-c", "x=1"], ["frontend"], "abc")
    assert calls["route"] == ("t", "old", str(home))
    assert (tmp_path / "route").read_text() == "status=routed\naccount=t\nplacement=open\n"
    assert (spec.slug, spec.agent.name, spec.agent.lane, spec.agent.task, spec.agent.seat) == (
        "sw",
        "engineer@abc-0001",
        "eng",
        "t1",
        "eng-1@sw",
    )
    assert (spec.agent.account, spec.agent.profile, spec.agent.model, spec.agent.overlays) == (
        "t",
        "engineer",
        "gpt-6.1-sol",
        ["frontend"],
    )
    assert spec.server_args == ("-c", 'model="gpt-6.1-sol"', "-c", "x=1")
    assert (spec.codex_home, spec.cwd, spec.environ["AH_CX_TOKEN_t"]) == (str(home), str(tmp_path), "x")
    assert (spec.previous_execution_id, spec.resume_thread, spec.opening) == ("exe-prev", "thread-kept", "Begin.")
    assert (spec.account_type, spec.allowed_tokens, spec.bundle_revision) == ("", frozenset({"AH_CX_TOKEN_t"}), "abc")


def supervised(monkeypatch, wait):
    process = SimpleNamespace(wait=wait, returncode=0)
    record = SimpleNamespace(
        execution_id="exe-1", thread_id="thread-1", endpoint_ref="unix:///run/cx/s", state="connected"
    )
    started = SimpleNamespace(record=record, process=process)
    seen = {"finished": [], "stopped": [], "signals": []}
    monkeypatch.setattr(serve, "prepare", lambda args, agent_args, environ: SimpleNamespace(slug="sw"))
    monkeypatch.setattr(serve, "connect", lambda environ: "store")
    monkeypatch.setattr(serve.signal, "signal", lambda signum, handler: seen["signals"].append((signum, handler)))
    monkeypatch.setattr(supervisor, "launch", lambda store, spec, deps: started)
    monkeypatch.setattr(supervisor, "stop", lambda proc: seen["stopped"].append(proc))
    monkeypatch.setattr(
        supervisor, "finish", lambda store, slug, done, now: seen["finished"].append((store, slug, done))
    )
    return started, seen


def test_main_supervises_the_server_until_it_exits(tmp_path, monkeypatch, capsys):
    started, seen = supervised(monkeypatch, lambda: 0)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 0
    assert "execution=exe-1 thread=thread-1 endpoint=unix:///run/cx/s state=connected" in capsys.readouterr().out
    assert seen["finished"] == [("store", "sw", started)]
    assert seen["stopped"] == [started.process]
    assert seen["signals"] == [
        (serve.signal.SIGTERM, serve._exit_on_signal),
        (serve.signal.SIGHUP, serve._exit_on_signal),
    ]


def test_a_signal_while_supervising_still_stops_the_server_and_finishes_the_record(tmp_path, monkeypatch):
    def interrupted():
        serve._exit_on_signal(serve.signal.SIGTERM, None)

    started, seen = supervised(monkeypatch, interrupted)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    with pytest.raises(SystemExit) as stopped:
        serve.main(argv, dict(SWARM_ENV))
    assert stopped.value.code == 128 + serve.signal.SIGTERM
    assert seen["stopped"] == [started.process]
    assert seen["finished"] == [("store", "sw", started)]


def test_main_reports_a_refused_launch(tmp_path, monkeypatch, capsys):
    def refused(store, spec, deps):
        raise supervisor.LaunchFailure("hooks_missing", "required hooks are missing")

    monkeypatch.setattr(serve, "prepare", lambda args, agent_args, environ: SimpleNamespace(slug="sw"))
    monkeypatch.setattr(serve, "connect", lambda environ: "store")
    monkeypatch.setattr(supervisor, "launch", refused)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 3
    assert "launch refused: required hooks are missing" in capsys.readouterr().err
