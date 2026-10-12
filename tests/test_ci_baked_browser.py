import os
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parents[1] / ".github/actions/browser-cache"


@pytest.mark.parametrize(
    "present,usable,existing",
    [(True, True, False), (True, False, False), (False, False, False), (True, True, True)],
)
def test_image_browser_is_used_only_after_a_successful_launch(tmp_path, present, usable, existing):
    baked = tmp_path / "baked"
    if present:
        (baked / "chromium_headless_shell-1234").mkdir(parents=True)
        (baked / "chromium-1234").mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / ".playwright"
    if existing:
        (target / "chromium-1234").mkdir(parents=True)
        (target / "chromium-1234" / "existing").write_text("preserved")
    output = tmp_path / "output"
    calls = tmp_path / "calls"
    python = tmp_path / "python"
    python.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'echo "$PLAYWRIGHT_BROWSERS_PATH" >> "$CALLS"\n'
        '[[ "$PLAYWRIGHT_BROWSERS_PATH" == "$ARC_PLAYWRIGHT_BROWSERS_PATH" ]]\n'
        '[[ "$USABLE" == true ]]\n'
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARC_PLAYWRIGHT_BROWSERS_PATH": str(baked),
        "PLAYWRIGHT_BROWSERS_PATH": str(workspace / ".playwright"),
        "GITHUB_ACTION_PATH": str(ACTION),
        "GITHUB_OUTPUT": str(output),
        "CALLS": str(calls),
        "USABLE": str(usable).lower(),
    }
    result = subprocess.run(["bash", str(ACTION / "use-image.sh")], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == f"ready={str(present and usable and not existing).lower()}"
    if present and not existing:
        assert calls.read_text().splitlines() == [str(baked)]
    else:
        assert not calls.exists()
    if existing:
        assert (target / "chromium-1234" / "existing").read_text() == "preserved"
        assert sorted(path.name for path in (target / "chromium-1234").iterdir()) == ["existing"]
    elif present and usable:
        assert (target / "chromium-1234").resolve() == baked / "chromium-1234"
        assert (target / "chromium_headless_shell-1234").resolve() == baked / "chromium_headless_shell-1234"
    else:
        assert not target.exists()


def test_usable_image_browser_skips_cache_restoration_and_duplicate_launch():
    steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]
    image = next(step for step in steps if step.get("id") == "image")
    cache = next(step for step in steps if step.get("id") == "cache")
    version = next(step for step in steps if step.get("id") == "version")
    launch = next(step for step in steps if step.get("id") == "launch")
    assert steps.index(image) < steps.index(cache)
    assert cache["if"] == "steps.image.outputs.ready != 'true'"
    assert version["if"] == "steps.image.outputs.ready != 'true'"
    assert launch["env"]["IMAGE_READY"] == "${{ steps.image.outputs.ready }}"
