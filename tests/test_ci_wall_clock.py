import textwrap
from pathlib import Path

import pytest


def test_discovers_an_unmarked_elapsed_bound_in_a_new_directory(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/new_area/test_new_budget.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "import time\n"
        "def test_new_budget():\n"
        "    started = time.perf_counter()\n"
        "    elapsed = (time.perf_counter() - started) / 20\n"
        "    assert elapsed < 0.005\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert (
        "tests/new_area/test_new_budget.py:5: test_new_budget requires pytest.mark.wall_clock"
        in capsys.readouterr().out
    )
    target.write_text(
        "import pytest\n"
        + target.read_text().replace("def test_new_budget", "@pytest.mark.wall_clock\ndef test_new_budget")
    )
    assert main(["--root", str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "source",
    [
        "import time\ndef test_budget():\n    start = time.monotonic()\n    assert time.monotonic() - start < 1",
        "from time import perf_counter as clock\nasync def test_budget():\n    start = clock()\n    elapsed = clock() - start\n    assert 0 <= elapsed <= 1",
        "import time as timer\nclass TestBudget:\n    def test_budget(self):\n        start = timer.time_ns()\n        self.assertLess(timer.time_ns() - start, 1000)",
        "import time\ndef measured():\n    start = time.monotonic()\n    return 'ok', time.monotonic() - start\ndef test_budget():\n    done, took = measured()\n    assert took < 1",
        "import time\ndef test_budget():\n    clock = time.perf_counter\n    start = clock()\n    assert clock() - start < 1",
        "import time\ndef test_budget():\n    starts = []\n    starts.append(time.monotonic())\n    assert all(later - earlier < 1 for earlier, later in zip(starts, starts[1:]))",
    ],
)
def test_rejects_real_elapsed_bounds_across_supported_test_forms(tmp_path, source, capsys):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(source)
    assert main(["--root", str(tmp_path)]) == 1
    assert "requires pytest.mark.wall_clock" in capsys.readouterr().out


@pytest.mark.parametrize(
    "marker",
    [
        "@pytest.mark.wall_clock",
        "@pytest.mark.wall_clock()",
        "pytestmark = pytest.mark.wall_clock",
        "pytestmark = [pytest.mark.wall_clock]",
    ],
)
def test_accepts_decorator_and_module_markers(tmp_path, marker):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        f"import time\nimport pytest\n{marker}\ndef test_budget():\n    start = time.monotonic()\n    assert time.monotonic() - start < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_accepts_an_inherited_class_marker(tmp_path):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        textwrap.dedent("""
        import time
        import pytest
        @pytest.mark.wall_clock
        class TestBudget:
            def test_budget(self):
                start = time.monotonic()
                assert time.monotonic() - start < 1
    """)
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_keeps_functional_result_checks_separate_from_elapsed_return_values(tmp_path):
    from scripts.ci_wall_clock import main

    target = tmp_path / "tests/test_budget.py"
    target.parent.mkdir()
    target.write_text(
        textwrap.dedent("""
        import time
        def measured():
            start = time.monotonic()
            return 0, time.monotonic() - start
        def test_result():
            result, elapsed = measured()
            assert result < 1
        def test_stored_duration():
            elapsed = 42
            assert elapsed < 50
        def test_ready_deadline():
            deadline = time.monotonic() + 5
            assert time.monotonic() < deadline
    """)
    )
    assert main(["--root", str(tmp_path)]) == 0


@pytest.mark.parametrize("clock_import", ["import time\nclock = time.monotonic", "from time import monotonic as clock"])
def test_follows_imported_test_helpers_without_importing_the_test_code(tmp_path, capsys, clock_import):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "measurement.py").write_text(
        clock_import + "\ndef measured():\n    start = clock()\n    return 'ok', clock() - start\n"
    )
    (directory / "test_budget.py").write_text(
        "from tests.measurement import measured\nraise RuntimeError('must not import')\ndef test_budget():\n    assert measured()[1] < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


def test_follows_clock_arguments_into_elapsed_helpers(tmp_path, capsys):
    from scripts.ci_wall_clock import main

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_budget.py").write_text(
        "import time\ndef elapsed(start):\n    return time.monotonic() - start\ndef test_budget():\n    start = time.monotonic()\n    assert elapsed(start) < 1\n"
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert "test_budget requires pytest.mark.wall_clock" in capsys.readouterr().out


@pytest.mark.parametrize("source", [None, "def test_broken("])
def test_refuses_an_empty_or_unparseable_test_tree(tmp_path, source):
    from scripts.ci_wall_clock import main

    if source is not None:
        directory = tmp_path / "tests"
        directory.mkdir()
        (directory / "test_broken.py").write_text(source)
    assert main(["--root", str(tmp_path)]) == 1


def test_lint_checks_the_head_tree_with_the_protected_timing_grader():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    lint = workflow["jobs"]["lint"]
    step = next(step for step in lint["steps"] if step.get("name") == "Require wall clock markers on elapsed bounds")
    assert step["if"] == "${{ !cancelled() }}"
    assert 'cd "$RUNNER_TEMP/grader"' in step["run"]
    assert 'python -m scripts.ci_wall_clock --root "$GITHUB_WORKSPACE"' in step["run"]
    assert "lint" in workflow["jobs"]["gate-required"]["needs"]
