from pathlib import Path

import pytest
import yaml
from coverage import CoverageData

from tests import coverage_baseline as cache


@pytest.fixture
def measured(tmp_path, monkeypatch):
    state = {"commit": "base", "tree": "tree"}
    monkeypatch.setattr(
        cache, "_git", lambda repo, revision: state["tree"] if revision.endswith("^{tree}") else state["commit"]
    )
    source = tmp_path / "hooks/example.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("value = 1\nvalue += 1\n")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    return tmp_path, state, [tmp_path / ".coverage"]


@pytest.mark.parametrize("invalid", [0, 4, 5, 6, 99])
def test_impossible_lines_cannot_replace_a_baseline(measured, invalid, capsys):
    root, _, shards = measured
    source = root / "hooks/example.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text('value = 1\nscript = """\nconst example = 1;\nconst other = 2;\n"""\n# comment\nvalue += 1\n')
    data = CoverageData(basename=str(shards[0]))
    data.erase()
    data.add_lines({"hooks/example.py": {1, 2, 7, invalid}})
    data.write()
    target = root / "baseline.json"
    target.write_text("existing baseline")
    with pytest.raises(ValueError, match="not executable"):
        cache.record(root, shards, target)
    assert f"hooks/example.py:{invalid}" in capsys.readouterr().out
    assert target.read_text() == "existing baseline"


def test_publication_reports_every_impossible_line(measured, capsys):
    root, _, shards = measured
    source = root / "hooks/example.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("value = 1\nvalue += 1\n")
    data = CoverageData(basename=str(shards[0]))
    data.erase()
    data.add_lines({"hooks/example.py": {1, 2, 99, 100}})
    data.write()
    with pytest.raises(ValueError, match="not executable"):
        cache.record(root, shards, root / "baseline.json")
    output = capsys.readouterr().out
    assert "hooks/example.py:99" in output
    assert "hooks/example.py:100" in output
    assert not (root / "baseline.json").exists()


def test_record_computes_the_baseline_from_coverage(measured):
    root, _, shards = measured
    target = root / "baseline.json"
    cache.record(root, shards, target)
    assert cache.read(target, "tree") == {
        "commit": "base",
        "tree": "tree",
        "shards": 1,
        "executed": {"hooks/example.py": [1, 2]},
        "history": [],
    }


def test_a_cache_miss_names_a_failed_dev_run_and_the_recovery(measured):
    root, _, _ = measured
    with pytest.raises(ValueError, match="failed dev run.*merge a measured dev revision.*rerunning"):
        cache.read(root / "missing.json", "tree")


def test_a_cache_for_another_tree_is_red(measured):
    root, _, files = measured
    target = root / "baseline.json"
    cache.record(root, files, target)
    with pytest.raises(ValueError, match="tree"):
        cache.read(target, "another")


def test_record_retains_the_previous_measurement_and_its_history(measured):
    root, state, shards = measured
    target = root / "baseline.json"
    cache.record(root, shards, target)
    state.update(commit="next", tree="next-tree")
    cache.record(root, shards, target, target)
    value = cache.read(target)
    assert value["commit"] == "next"
    assert [item["commit"] for item in value["history"]] == ["base"]


def test_an_incomplete_suite_cannot_publish_a_baseline(measured):
    root, _, shards = measured
    target = root / "baseline.json"
    with pytest.raises(ValueError, match="shard coverage missing"):
        cache.record(root, [*shards, root / "missing.coverage"], target)
    assert not target.exists()


@pytest.mark.parametrize("folders,code", [(2, 0), (3, 1)], ids=["exact", "extra-shard"])
def test_the_recorder_reads_exactly_the_shards_the_run_split(measured, folders, code):
    root, _, _ = measured
    for index in range(1, folders + 1):
        folder = root / "downloaded" / f"coverage-3.12-{index}"
        folder.mkdir(parents=True)
        data = CoverageData(basename=str(folder / ".coverage"))
        data.add_lines({"hooks/example.py": {index}})
        data.write()
    target = root / "baseline.json"
    argv = ["--head", str(root), "--shards", "2", "--head-shards", str(root / "downloaded"), "--out", str(target)]
    assert cache.main(argv) == code
    assert target.exists() == (code == 0)


def test_history_is_bounded_to_thirty_measurements(measured):
    root, state, shards = measured
    target = root / "baseline.json"
    for index in range(35):
        state.update(commit=str(index), tree=str(index))
        cache.record(root, shards, target, target)
    assert [item["commit"] for item in cache.read(target)["history"]] == [str(index) for index in range(33, 3, -1)]


def test_publisher_checks_parent_results_without_inheriting_skips():
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    assert job["needs"] == ["unit", "gate-required"]
    assert job["if"] == "${{ !cancelled() && github.event_name == 'push' && needs.unit.result == 'success' }}"


def test_publisher_reads_shards_without_the_optional_sonar_report():
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    steps = {step.get("name"): step for step in job["steps"]}
    download = steps["Download this run's measured coverage"]["with"]
    assert download["pattern"] == "coverage-3.12-*"
    assert "name" not in download
    assert f"--head-shards {download['path']} " in steps["Compute the coverage baseline"]["run"]
