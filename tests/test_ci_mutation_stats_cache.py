import subprocess
from pathlib import Path

import pytest


def test_unchanged_test_stats_are_reused_and_only_the_changed_file_is_collected(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "scripts/sample.py").write_text("def sample():\n    return 1\n")
    files = ["tests/test_a.py", "tests/test_b.py"]
    for path in files:
        (root / path).write_text("def test_sample():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "scripts/sample.py", *files], cwd=root, check=True)
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.sample.x_sample": [f"{path}::test_sample" for path in chosen]},
                "durations": {f"{path}::test_sample": 1 for path in chosen},
                "cpu": 2,
            }
        ]

    cache = tmp_path / "cache"
    changes = {"scripts/sample.py": {"lines": [2]}}
    first = collect_cached_stats(root, cache, changes, files, ["-q"], collect)
    assert calls == [files]
    warm = collect_cached_stats(root, cache, changes, files, ["-q"], collect)
    assert calls == [files]
    assert sum(result["cpu"] for result in warm) == sum(result["cpu"] for result in first)
    assert {test for result in warm for test in result["durations"]} == {
        "tests/test_a.py::test_sample",
        "tests/test_b.py::test_sample",
    }
    assert {test for result in first for test in result["tests"]["scripts.sample.x_sample"]} == {
        "tests/test_a.py::test_sample",
        "tests/test_b.py::test_sample",
    }
    (root / files[1]).write_text("def test_sample():\n    assert True\n")
    collect_cached_stats(root, cache, changes, files, ["-q"], collect)
    assert calls == [files, ["tests/test_b.py"]]


def test_failed_collection_never_populates_reusable_stats(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    test = root / "test_sample.py"
    test.write_text("def test_sample():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    cache = tmp_path / "cache"
    with pytest.raises(SystemExit, match="1"):
        collect_cached_stats(
            root,
            cache,
            {},
            ["test_sample.py"],
            [],
            lambda files: [{"status": 1, "tests": {}, "durations": {}, "cpu": 1}],
        )
    assert list(cache.glob("*.json")) == []


def test_collection_parts_reuse_stats_and_keep_the_new_head_identity(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import load_or_collect_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "scripts/sample.py").write_text("def sample():\n    return 1\n")
    (root / "test_sample.py").write_text("def test_sample():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "scripts/sample.py", "test_sample.py"], cwd=root, check=True)
    (root / "changed-lines.json").write_text(
        json.dumps({"scripts/sample.py": {"lines": [2], "tests": ["test_sample.py"]}})
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(root))
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("os.sched_getaffinity", lambda pid: {0})
    config = SimpleNamespace(source_paths=[Path("scripts")], pytest_add_cli_args_test_selection=["test_sample.py"])
    runner = SimpleNamespace(
        SourceFileMutationData=lambda **kwargs: SimpleNamespace(load=lambda: None, exit_code_by_key={"m": None}),
        Config=SimpleNamespace(get=lambda: config),
    )
    calls = []
    result = {
        "status": 0,
        "tests": {"scripts.sample.x_sample": ["test_sample.py::test_sample"]},
        "durations": {"test_sample.py::test_sample": 1},
        "cpu": 2,
    }

    def collect(engine, test_runner, buckets, work):
        calls.append(buckets)
        return [result]

    monkeypatch.setattr("scripts.ci_mutation.selection.run_stats_buckets", collect)
    monkeypatch.setattr("scripts.ci_mutation.selection.stats_shards", lambda root, files, workers: [files])
    for head in ("old", "new"):
        output = root / f"{head}.json"
        with pytest.raises(SystemExit, match="0"):
            load_or_collect_stats(
                runner,
                SimpleNamespace(_pytest_add_cli_args=["-q"]),
                ["scripts/sample.py"],
                "collect",
                (str(output), head, "0", "1"),
            )
        assert json.loads(output.read_text())["key"] == head
    assert calls == [[["test_sample.py"]]]


@pytest.mark.parametrize(
    "changed",
    [
        "source",
        "lines",
        "fixture",
        "imported_fixture",
        "imported_fixture_through_helper",
        "options",
        "runtime",
        "dependency",
    ],
)
def test_incompatible_source_fixture_and_runtime_stats_are_recollected(tmp_path, monkeypatch, changed):
    import sys
    from importlib import metadata
    from types import SimpleNamespace

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    source = root / "scripts/sample.py"
    source.write_text("def sample():\n    return 1\n")
    test = root / "tests/test_sample.py"
    test.write_text("from tests.test_shared import value\ndef test_sample():\n    assert value == 1\n")
    fixture = root / "tests/conftest.py"
    fixture.write_text("value = 1\n")
    shared = root / "tests/test_shared.py"
    shared.write_text("value = 1\n")
    if changed == "imported_fixture_through_helper":
        (root / "tests/helper.py").write_text("from tests.test_shared import value\n")
        test.write_text("from tests.helper import value\ndef test_sample():\n    assert value == 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "add", "scripts/sample.py", "tests/test_sample.py", "tests/conftest.py", "tests/test_shared.py"],
        cwd=root,
        check=True,
    )
    if changed == "imported_fixture_through_helper":
        subprocess.run(["git", "add", "tests/helper.py"], cwd=root, check=True)
    monkeypatch.setattr(metadata, "distributions", lambda: [SimpleNamespace(metadata={"Name": "sample"}, version="1")])
    calls = []
    files = ["tests/test_sample.py"]

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.sample.x_sample": ["tests/test_sample.py::test_sample"]},
                "durations": {"tests/test_sample.py::test_sample": 1},
                "cpu": 2,
            }
        ]

    changes = {"scripts/sample.py": [2]}
    options = ["-q"]
    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, changes, files, options, collect)
    if changed == "source":
        source.write_text("def sample():\n    return 2\n")
    elif changed == "lines":
        changes = {"scripts/sample.py": [1, 2]}
    elif changed == "fixture":
        fixture.write_text("value = 2\n")
    elif changed in ("imported_fixture", "imported_fixture_through_helper"):
        shared.write_text("value = 2\n")
    elif changed == "options":
        options = ["-x"]
    elif changed == "runtime":
        monkeypatch.setattr(sys, "version", "changed")
    else:
        monkeypatch.setattr(
            metadata, "distributions", lambda: [SimpleNamespace(metadata={"Name": "sample"}, version="2")]
        )
    collect_cached_stats(root, cache, changes, files, options, collect)
    assert calls == [files, files]


def test_persisted_stats_reuse_survives_a_new_python_process(tmp_path):
    import json
    import os
    import sys

    root = tmp_path / "repo"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "tests/test_sample.py").write_text(
        "\n".join(f"from tests.test_shared_{index} import value" for index in range(5)) + "\n"
    )
    for index in range(5):
        (root / f"tests/test_shared_{index}.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "add", "tests/test_sample.py", *(f"tests/test_shared_{index}.py" for index in range(5))],
        cwd=root,
        check=True,
    )
    script = """
import json
import sys
from pathlib import Path
from scripts.ci_mutation.stats_cache import collect_cached_stats

root, folder = map(Path, sys.argv[1:])
calls = root / "collections.json"
def collect(files):
    calls.write_text(json.dumps(json.loads(calls.read_text()) + [files] if calls.exists() else [files]))
    return [{"status": 0, "tests": {"scripts.sample.x_sample": ["tests/test_sample.py::test_sample"]}, "durations": {"tests/test_sample.py::test_sample": 1}, "cpu": 2}]
results = collect_cached_stats(root, folder, {"scripts/sample.py": [2]}, ["tests/test_sample.py"], ["-q"], collect)
assert sum(result["cpu"] for result in results) == 2
assert {test for result in results for test in result["durations"]} == {"tests/test_sample.py::test_sample"}
"""
    for seed in ("0", "42"):
        subprocess.run(
            [sys.executable, "-c", script, str(root), str(tmp_path / "cache")],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            capture_output=True,
            text=True,
            check=True,
        )
    assert json.loads((root / "collections.json").read_text()) == [["tests/test_sample.py"]]
