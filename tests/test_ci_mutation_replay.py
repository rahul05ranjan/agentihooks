import json
from zipfile import ZipFile

import pytest

from scripts.ci_mutation import replay


@pytest.mark.parametrize("changed_scope", [False, True])
def test_replay_preserves_sources_tests_and_selected_mutants(tmp_path, monkeypatch, changed_scope):
    archive = tmp_path / "input.zip"
    name = "scripts.sample.x_f__mutmut_1"
    scope = {"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}}
    stats = [
        {
            "tests": {"scripts.sample.x_f": ["tests/test_sample.py::test_f"]},
            "durations": {"tests/test_sample.py::test_f": 100.0},
        }
    ]
    with ZipFile(archive, "w") as zipfile:
        for path, content in {
            "changed-lines.json": json.dumps(scope),
            "shared-stats.json": json.dumps(stats),
            "scripts/sample.py": "def f():\n    return 1\n",
            "tests/test_sample.py": "from scripts.sample import f\n",
            "mutants/scripts/sample.py.meta": json.dumps({"exit_code_by_key": {name: None}}),
            "killers/old.json": "{}",
        }.items():
            zipfile.writestr(f"old/{path}", content)
        zipfile.writestr("old/scripts/", "")
        zipfile.writestr("unrelated.txt", "ignored")
    downloaded = []

    def download(command, *, stdout, check):
        downloaded.append((command, check))
        stdout.write(archive.read_bytes())

    monkeypatch.setattr(replay.subprocess, "run", download)
    monkeypatch.setattr(
        "scripts.ci_mutation.measured.selected_mutants",
        lambda *args: ("", ["x_other__mutmut_1" if changed_scope else "x_f__mutmut_1"]),
    )
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    folder = tmp_path / "replay"
    cwd = __import__("pathlib").Path.cwd()
    if changed_scope:
        with pytest.raises(ValueError, match="changed the retained mutant scope"):
            replay.prepare("second", folder)
    else:
        plan = replay.prepare("second", folder)
        assert plan["assignments"] == {name: 0}
        assert plan["costs"][name] >= 100
        manifest = json.loads((folder / "manifest.json").read_text())
        assert manifest["case"] == "second"
        assert manifest["artifact"] == 11756407264
        assert manifest["mutants"] == 1
        assert set(manifest["source_test_sha256"]) == {"scripts/sample.py", "tests/test_sample.py"}
        assert (folder / "source/scripts/sample.py").read_text() == "def f():\n    return 1\n"
        assert not (folder / "source/mutants").exists()
        assert not (folder / "source/killers").exists()
        assert output.read_text() == "shards=[0]\n"
    assert __import__("pathlib").Path.cwd() == cwd
    assert downloaded == [
        (["gh", "api", "repos/the-cloud-clockwork/agentihooks/actions/artifacts/11756407264/zip"], True)
    ]


@pytest.mark.parametrize("fault", ["", "missing", "budget", "ungraded", "wrong count"])
def test_replay_accepts_complete_survivor_failures_and_rejects_incomplete_grades(tmp_path, monkeypatch, fault):
    (tmp_path / "stats").mkdir()
    (tmp_path / "grades/shard-0").mkdir(parents=True)
    (tmp_path / "manifest.json").write_text(json.dumps({"case": "third", "mutants": 1}))
    (tmp_path / "stats/plan.json").write_text(json.dumps({"shards": 1}))
    file = {
        "path": "scripts/sample.py",
        "counts": {"survived": 1},
        "failures": [{"name": "survivor"}],
        "cleared": [],
        "untouched_survivors": [],
    }
    report = {
        "files": [file],
        "not_mutated": [],
        "failed": True,
        "shard": {
            "index": 0,
            "expected": 2 if fault == "wrong count" else 1,
            "graded": 0 if fault == "ungraded" else 1,
            "complete": fault != "ungraded",
            "budget_exhausted": fault == "budget",
        },
    }
    if fault != "missing":
        (tmp_path / "grades/shard-0/report.json").write_text(json.dumps(report))
    monkeypatch.setattr("sys.argv", ["replay", "verify", "--folder", str(tmp_path)])
    assert replay.main() == (0 if not fault else 1)
    proof = json.loads((tmp_path / "replay-proof.json").read_text())
    assert proof["case"] == "third"
    assert proof["expected"] == 1
    assert proof["graded"] == (0 if fault in {"missing", "ungraded"} else 1)
    assert proof["exhausted_shards"] == (1 if fault == "budget" else 0)
    assert proof["survivor_grade_failed"] is (fault != "missing")
    assert proof["complete"] is (fault not in {"missing", "ungraded", "wrong count"})
