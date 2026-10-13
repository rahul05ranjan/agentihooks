import subprocess


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
