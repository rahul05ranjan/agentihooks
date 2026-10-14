import argparse

import pytest
from coverage import CoverageData

from tests import coverage_baseline as cache
from tests import coverage_ratchet as ratchet


@pytest.fixture
def measured(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_git", lambda repo, revision: "tree" if revision.endswith("^{tree}") else "base")
    monkeypatch.setattr(ratchet, "git", lambda *args, **kwargs: "tree" if args[-1].endswith("^{tree}") else "base")
    monkeypatch.setattr("tests.coverage_history._show", lambda repo, commit: lambda path: "one\ntwo\nthree\n")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/example.py").write_text("one\ntwo\nthree\n")
    target = tmp_path / "baseline.json"
    cache.record(tmp_path, [tmp_path / ".coverage"], target)
    return tmp_path, target


def test_cached_base_uses_the_requested_commit_with_the_same_tree(measured):
    root, target = measured
    base = next(ratchet._cached_runs(root, "squash", target))
    assert base.commit == "squash"
    assert base.executed == {"hooks/example.py": {1, 2}}
    assert base.source("hooks/example.py") == "one\ntwo\nthree\n"


@pytest.mark.parametrize("covered,lost", [({1, 2}, {}), ({1}, {"hooks/example.py": [2]})])
def test_cached_grading_does_not_read_github(measured, monkeypatch, covered, lost):
    root, target = measured
    shard = root / ".coverage"
    data = CoverageData(basename=str(shard))
    data.add_lines({"hooks/example.py": covered})
    data.write()
    monkeypatch.setattr(ratchet, "renamed", lambda *args: {})
    monkeypatch.setattr(ratchet, "dev_runs", lambda *args: pytest.fail("GitHub history read"))
    args = argparse.Namespace(head=root, base="base", shards=1, base_baseline=target)
    _, _, result = ratchet._grade(args, [shard], root)
    assert result.lost == lost


def test_a_head_split_another_way_grades_against_the_cached_base(measured, monkeypatch):
    root, target = measured
    shards = []
    for index, covered in enumerate([{1}, {2}], start=1):
        data = CoverageData(basename=str(root / f"shard-{index}.coverage"))
        data.add_lines({"hooks/example.py": covered})
        data.write()
        shards.append(root / f"shard-{index}.coverage")
    monkeypatch.setattr(ratchet, "renamed", lambda *args: {})
    args = argparse.Namespace(head=root, base="base", shards=len(shards), base_baseline=target)
    _, _, result = ratchet._grade(args, shards, root)
    assert result.lost == {}


def test_the_ratchet_refuses_a_cached_base_from_another_tree(measured, monkeypatch):
    root, target = measured
    monkeypatch.setattr(ratchet, "git", lambda *args, **kwargs: "another-tree")
    with pytest.raises(ValueError, match="tree differs"):
        next(ratchet._cached_runs(root, "base", target))


def test_cache_miss_fails_the_cli_with_recovery_guidance(measured, capsys):
    root, _ = measured
    folder = root / "coverage-3.12-1"
    folder.mkdir()
    data = CoverageData(basename=str(folder / ".coverage"))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    result = ratchet.main(
        [
            "--head",
            str(root),
            "--base",
            "base",
            "--shards",
            "1",
            "--head-shards",
            str(root),
            "--out",
            str(root / "report"),
            "--base-baseline",
            str(root / "missing.json"),
        ]
    )
    assert result == 1
    assert "failed dev run" in capsys.readouterr().out
    assert not (root / "report/report.txt").exists()


def test_queue_grading_consumes_the_restored_exact_base():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    job = workflow["jobs"]["coverage-ratchet"]
    assert "queue-baseline" in job["needs"]
    assert "!cancelled()" in job["if"]
    steps = {step.get("name"): step for step in job["steps"]}
    base = steps["Resolve the measured base tree"]
    assert base["env"]["QUEUE_BASE"] == "${{ github.event.merge_group.base_sha }}"
    assert "git merge-base" not in base["run"]
    restore = steps["Restore the exact coverage baseline"]
    assert restore["if"] == "github.event_name != 'merge_group' && github.event_name != 'workflow_dispatch'"
    download = steps["Download the queue base coverage baseline"]
    assert download["if"] == "github.event_name == 'merge_group' || github.event_name == 'workflow_dispatch'"
    assert download["with"]["name"] == "queue-coverage-baseline"
    assert download["with"]["path"] == "~/coverage-baseline"
    assert "git merge-base" not in steps["Hold every line the base ran"]["run"]


@pytest.mark.parametrize(
    ("first", "second", "cached", "expected"),
    [
        (False, True, False, "22"),
        (True, False, False, "11"),
        (False, False, False, "11"),
        (False, True, True, "22"),
        (False, False, True, None),
    ],
)
def test_base_lookup_prefers_history_and_keeps_an_exact_base_fallback(tmp_path, first, second, cached, expected):
    import json
    import os
    import subprocess
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    step = next(s for s in workflow["jobs"]["coverage-ratchet"]["steps"] if s.get("id") == "base-run")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == run && "$2" == download ]]; then\n'
        '  mkdir -p "$7"\n'
        '  history="$SECOND"; [[ "$3" != 11 ]] || history="$FIRST"\n'
        '  printf \'{"history":%s}\\n\' "$history" > "$7/baseline.json"\n'
        "  exit 0\n"
        "fi\n"
        'case "$2" in\n'
        '  */runs\\?head_sha=dev\\&status=success\\&per_page=20) printf "11\\n22\\n" ;;\n'
        "  */artifacts*) echo 1 ;;\n"
        "  *) exit 7 ;;\n"
        "esac\n"
    )
    (tools / "gh").chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = dict(
        os.environ,
        PATH=f"{tools}:{os.environ['PATH']}",
        BASE="dev",
        CACHED=str(cached).lower(),
        RUNNER_TEMP=str(tmp_path),
        FIRST=json.dumps([{"commit": "older"}] if first else []),
        SECOND=json.dumps([{"commit": "older"}] if second else []),
        GITHUB_REPOSITORY="the-cloud-clockwork/agentihooks",
        GITHUB_OUTPUT=str(output),
    )
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == (f"id={expected}\n" if expected else "")
    if cached:
        mint = next(s for s in workflow["jobs"]["coverage-ratchet"]["steps"] if s.get("id") == "app-token")
        assert "steps.cached-history.outputs.retained" in mint["if"]
    if "gh run download" in step["run"]:
        assert '-R "$GITHUB_REPOSITORY"' in step["run"]


@pytest.mark.parametrize("retained", [False, True])
def test_cache_skips_lookup_only_when_history_is_retained(tmp_path, retained):
    import json
    import os
    import subprocess
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    step = next(s for s in workflow["jobs"]["coverage-ratchet"]["steps"] if s.get("id") == "cached-history")
    folder = tmp_path / "coverage-baseline"
    folder.mkdir()
    (folder / "baseline.json").write_text(json.dumps({"history": [{"commit": "older"}] if retained else []}))
    output = tmp_path / "output"
    output.write_text("")
    env = dict(os.environ, HOME=str(tmp_path), GITHUB_OUTPUT=str(output))
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"retained={str(retained).lower()}\n"
    assert step["if"] == "steps.cached.outputs.cache-hit == 'true'"
