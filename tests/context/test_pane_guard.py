import pytest

from hooks import hook_manager
from hooks.context.pane_guard import opens_pane
from hooks.context.shell_commands import commands

OPENERS = [
    "herdr pane split --direction right",
    "herdr --machine anton-hive pane run w1:p1 'agentihooks balance'",
    "herdr tab create",
    "herdr workspace create",
    "agentihooks run-in-terminal 'npm run build' --dir ~/dev/project",
    "agentihooks init-agent --dir ~/dev/project --name engineer-a",
    "agentihooks swarm demo master up",
    "bash skills/prompt-user-parameter/scripts/open_param_pane.sh script.sh title",
]


def payload(command: str) -> dict:
    return {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}


@pytest.mark.parametrize("command", OPENERS)
def test_pane_openers_pass_the_shell_guards_without_any_typed_phrase(command):
    assert any(opens_pane(tokens) for tokens in commands(command))
    hook_manager._check_shell_guards(payload(command))
