import json
import os
import time
from pathlib import Path

import pytest
import tomlkit

from scripts.ci_mutation import runner
from scripts.ci_mutation.stats import SharedStats


@pytest.mark.parametrize("field_cap", [500, 4096, 100000])
def test_replay_collects_clean_statistics(tmp_path, monkeypatch, field_cap):
    monkeypatch.setenv("LANGFUSE_FIELD_MAX_CHARS", str(field_cap))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "agent" * 1000)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "swarm" * 1000)
    monkeypatch.setenv("USER", "user" * 1000)
    prepare = runner.prepare_workspace

    def focused_workspace(root, work, paths, tests):
        prepare(root, work, paths, tests)
        project = tomlkit.parse((work / "pyproject.toml").read_text())
        project["tool"]["mutmut"]["pytest_add_cli_args"] += ["-k", "test_stopped_codex_cursor_replays_all_pages_once"]
        (work / "pyproject.toml").write_text(tomlkit.dumps(project))

    monkeypatch.setattr(runner, "prepare_workspace", focused_workspace)
    selected = {"hooks/observability/agent_trace.py": ({164}, ["tests/observability/test_trace_flush.py"])}
    stats = SharedStats(tmp_path / "stats", "replay-proof", (0, 1))
    rows, reason = runner.mutate_files(
        Path(__file__).resolve().parents[2], tmp_path / "work", selected, time.monotonic() + 180, (0, 1), stats
    )
    log = (tmp_path / "work/run.log").read_text()
    print(log)
    assert reason == "", reason
    assert rows == {"hooks/observability/agent_trace.py": []}
    parts = list(stats.folder.glob("part-*.json"))
    assert len(parts) == 1
    part = json.loads(parts[0].read_text())
    print(json.dumps(part))
    assert "test_stopped_codex_cursor_replays_all_pages_once" in json.dumps(part)
    for evidence in (tmp_path / "work").glob("stats-*.json"):
        assert json.loads(evidence.read_text())["status"] == 0
    assert os.environ["LANGFUSE_FIELD_MAX_CHARS"] == str(field_cap)
