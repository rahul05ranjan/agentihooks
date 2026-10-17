from pathlib import Path


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


def test_every_real_elapsed_bound_in_the_repository_is_marked():
    from scripts.ci_wall_clock import main

    assert main(["--root", str(Path(__file__).resolve().parents[1])]) == 0
