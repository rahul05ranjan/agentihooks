import argparse
import shlex
import sys

import pytest

from scripts import init_agent

pytestmark = pytest.mark.unit


def app_server_spec(**changes):
    return init_agent.AgentSpec(
        agent="codex",
        profile="engineer",
        transport=init_agent.APP_SERVER,
        seat="eng-1@sw",
        **changes,
    )


def launched(tmp_path, spec, prompt="Open the task."):
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "engineer@abc-0001", prompt, ["-m", "gpt-6.1-sol"], {"XDG_RUNTIME_DIR": str(tmp_path)}, spec
    )
    line = next(line for line in launcher.read_text().splitlines() if "scripts.codex_session" in line)
    return launcher, prompt_file, shlex.split(line)


def test_the_app_server_branch_runs_the_supervisor_with_the_opening_prompt_file(tmp_path):
    spec = app_server_spec(
        overlays=("frontend",), bundle_revision="abc", previous_execution="exe-prev", resume="thread-kept"
    )
    launcher, prompt_file, argv = launched(tmp_path, spec)
    split = argv.index("--")
    assert argv[:4] == [sys.executable, "-m", "scripts.codex_session", "serve"]
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[argv.index("--profile") + 1] == "engineer"
    assert argv[argv.index("--seat") + 1] == "eng-1@sw"
    assert "--overlay=frontend" in argv and "--bundle-revision=abc" in argv
    assert argv[argv.index("--previous-execution") + 1] == "exe-prev"
    assert argv[argv.index("--resume") + 1] == "thread-kept"
    assert argv[argv.index("--report") + 1] == str(init_agent._route_report(launcher))
    assert prompt_file.suffix == ".opening"
    assert argv[argv.index("--prompt-file") + 1] == str(prompt_file)
    assert argv[-2:] == ["-m", "gpt-6.1-sol"]
    assert split < len(argv) - 2
    assert "--no-daemon" not in argv


def test_the_app_server_branch_never_hands_the_prompt_to_the_pane(tmp_path):
    launcher, prompt_file, argv = launched(tmp_path, app_server_spec())
    text = launcher.read_text()
    assert init_agent.PROMPT_AFTER_START not in text
    assert "$(cat" not in text
    assert init_agent._held_prompt(launcher, "codex") == ""
    assert prompt_file.read_text(encoding="utf-8") == "Open the task."


def test_the_app_server_branch_passes_the_interactive_route(tmp_path):
    _, _, argv = launched(tmp_path, app_server_spec(auth="interactive"))
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["--route", "interactive"]


def test_a_legacy_codex_launch_keeps_the_interactive_terminal(tmp_path):
    launcher, prompt_file = init_agent._write_launcher(
        tmp_path, "m", "Open.", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent="codex")
    )
    assert "agentihooks codex " in launcher.read_text()
    assert "scripts.codex_session" not in launcher.read_text()
    assert prompt_file.suffix == ".prompt"


def namespace(**changes):
    values = {"codex_transport": init_agent.APP_SERVER, "seat": "eng-1@sw", "profile": "engineer"}
    return argparse.Namespace(**{**values, **changes})


def test_the_transport_flag_needs_codex_a_seat_and_a_profile():
    assert init_agent._transport(namespace(), "codex") == init_agent.APP_SERVER
    assert init_agent._transport(namespace(codex_transport=""), "claude") == ""
    assert init_agent._transport(namespace(codex_transport="legacy"), "codex") == "legacy"
    with pytest.raises(ValueError, match="runs only a Codex agent"):
        init_agent._transport(namespace(), "claude")
    for missing in ("seat", "profile"):
        with pytest.raises(ValueError, match="needs --seat and --profile"):
            init_agent._transport(namespace(**{missing: ""}), "codex")


def test_the_parser_takes_the_transport_seat_and_predecessor():
    args = init_agent._parser().parse_args(
        ["--codex-transport", "app_server", "--seat", "eng-1@sw", "--previous-execution", "exe-prev"]
    )
    assert (args.codex_transport, args.seat, args.previous_execution) == ("app_server", "eng-1@sw", "exe-prev")
    with pytest.raises(SystemExit):
        init_agent._parser().parse_args(["--codex-transport", "pane"])
