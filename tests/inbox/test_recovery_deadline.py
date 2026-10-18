import json
import subprocess
import sys
from pathlib import Path

PROBE = """
import json, signal, sys

from tests.inbox import conftest, test_channel_reconnect

armed = []


def arm(which, seconds):
    server = sys.modules.get("mcp.server")
    armed.append({"seconds": seconds, "stdio": hasattr(server, "stdio"), "message": "mcp.shared.message" in sys.modules})


signal.setitimer = arm
names = [name for mark in test_channel_reconnect.pytestmark if mark.name == "usefixtures" for name in mark.args]
for name in names:
    next(getattr(conftest, name)._get_wrapped_function()())
print(json.dumps(armed))
"""


def test_the_channel_reconnect_alarm_is_armed_only_after_the_sdk_is_loaded():
    result = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=Path(__file__).parents[2], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [{"seconds": 5, "stdio": True, "message": True}]
