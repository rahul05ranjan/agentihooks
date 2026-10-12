import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.ci_mutation import killers, selection

TESTS = ["tests/test_a.py::fast", "tests/test_b.py::middle", "tests/test_c.py::slow"]


def _report(nodeid, failed=True):
    return SimpleNamespace(nodeid=nodeid, failed=failed)


def test_a_function_without_a_recorded_kill_keeps_the_duration_order(tmp_path):
    memory = killers.KillMemory(tmp_path / "killers")
    assert memory.order("scripts.a.x_f__mutmut_1", TESTS) == TESTS


def test_the_test_that_killed_a_mutant_runs_first_for_every_later_mutant_of_its_function(tmp_path):
    memory = killers.KillMemory(tmp_path / "killers")
    memory.mutant = "scripts.a.x_f__mutmut_1"
    memory.pytest_runtest_logreport(_report("tests/test_b.py::middle", failed=False))
    memory.pytest_runtest_logreport(_report("tests/test_c.py::slow"))
    memory.pytest_runtest_logreport(_report("tests/test_a.py::fast"))
    assert memory.mutant is None
    later = killers.KillMemory(tmp_path / "killers")
    assert later.order("scripts.a.x_f__mutmut_7", TESTS) == [
        "tests/test_c.py::slow",
        "tests/test_a.py::fast",
        "tests/test_b.py::middle",
    ]
    assert later.order("scripts.a.x_g__mutmut_1", TESTS) == TESTS
    assert later.order("scripts.a.x_f__mutmut_2", TESTS[:2]) == TESTS[:2]
    assert sorted(path.name for path in (tmp_path / "killers").iterdir()) == ["scripts.a.x_f"]


def test_a_report_outside_a_mutant_run_records_nothing(tmp_path):
    memory = killers.KillMemory(tmp_path / "killers")
    memory.pytest_runtest_logreport(_report("tests/test_c.py::slow"))
    assert not (tmp_path / "killers").exists()


def test_the_selection_runner_orders_by_kills_and_records_them_only_for_mutants(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(json.dumps({"scripts/a.py": {"lines": [2], "tests": ["tests/test_a.py"]}}))
    runs = []

    class PytestRunner:
        def execute_pytest(self, params, **kwargs):
            plugins = kwargs.get("plugins", [])
            runs.append((list(params), len(plugins)))
            for plugin in plugins:
                plugin.pytest_runtest_logreport(_report(params[-1]))
            return 1

        def run_tests(self, *, mutant_name, tests):
            return self.execute_pytest(list(tests), plugins=[])

    def cli(args):
        test_runner = runner.PytestRunner()
        test_runner.run_tests(mutant_name="scripts.a.x_f__mutmut_1", tests=TESTS)
        test_runner.run_tests(mutant_name="scripts.a.x_f__mutmut_2", tests=TESTS)
        test_runner.run_tests(mutant_name=None, tests=["tests/test_x.py::t"])

    runner = SimpleNamespace(
        collect_source_file_mutation_data=None,
        PytestRunner=PytestRunner,
        mutmut=SimpleNamespace(),
        cli=cli,
    )
    monkeypatch.setitem(sys.modules, "mutmut", SimpleNamespace(__main__=runner))
    monkeypatch.setenv("CI", "true")
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.setitem(sys.modules, name, sys.modules[name])
    selection.run_selected(selection_file, (0, 1))
    assert runs == [
        (TESTS, 1),
        (["tests/test_c.py::slow", "tests/test_a.py::fast", "tests/test_b.py::middle"], 1),
        (["tests/test_x.py::t"], 0),
    ]
    assert (tmp_path / "killers" / "scripts.a.x_f").read_text() == "tests/test_c.py::slow"
    assert not list((tmp_path / "killers").glob(f"*.{os.getpid()}"))
    assert Path.cwd() == tmp_path
