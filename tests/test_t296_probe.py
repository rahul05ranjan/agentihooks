import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPT = r"""
import sys, pytest
files = open(sys.argv[1]).read().split()
banner = "tests/test_quota_usage.py::test_native_rate_limits_render_requested_banner"
def state():
    m = sys.modules.get("hooks.context.quota_usage")
    c = sys.modules.get("hooks.config")
    return (id(m) if m else None, getattr(m, "QUOTA_USAGE_TOOL_CALLS", None), getattr(c, "QUOTA_USAGE_TOOL_CALLS", None))
prev = state()
print("START", prev, flush=True)
for f in files:
    pytest.main(["-q", "-p", "no:xdist", "-p", "no:cacheprovider", "-p", "no:randomly", "-x", "--no-header", "-o", "console_output_style=classic", f, "--deselect", banner])
    now = state()
    if now != prev:
        print("CHANGE", f, prev, "->", now, flush=True)
    prev = now
rc = pytest.main(["-q", "-p", "no:xdist", "-p", "no:cacheprovider", banner])
print("BANNER_RC", rc, state(), flush=True)
"""


def test_probe(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(ROOT / "tests" / "t296_gw2_files.txt")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=1500,
    )
    lines = [x for x in result.stdout.splitlines() if x.startswith(("START", "CHANGE", "BANNER_RC"))]
    assert False, "\n".join(lines) + "\n" + result.stderr[-1500:]
