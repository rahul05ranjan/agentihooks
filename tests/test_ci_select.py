import json
import subprocess

import pytest

from scripts import ci_select

pytestmark = pytest.mark.unit


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "dev")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    git(tmp_path, "config", "user.name", "CI")
    write(
        tmp_path,
        {
            "hooks/sample.py": "VALUE = 1\n",
            "scripts/lonely.py": "VALUE = 2\n",
            "tests/test_sample.py": "from hooks import sample\n",
            "tests/test_page.py": "PAGE = 'ledger.js'\n",
            "tests/test_other.py": "import os\n",
            ".test_durations": "{}",
        },
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def decide(root, changed, durations=None, limit=3):
    return ci_select.decide(root, changed, durations or {}, limit)


@pytest.mark.parametrize(
    "changed",
    [
        ["pyproject.toml"],
        [".github/test-excludes.txt"],
        ["tests/__init__.py"],
        ["tests/shards.py"],
        ["tests/flake_ledger.py"],
        ["tests/browser_coverage.py"],
        ["tests/node_capture.cjs"],
        ["tests/conftest.py"],
        ["tests/swarm/conftest.py", "tests/test_sample.py"],
    ],
)
def test_a_change_to_the_test_harness_runs_the_full_suite(repo, changed):
    assert decide(repo, changed) == ("full", [])


def test_a_source_change_selects_the_tests_that_import_it(repo):
    assert decide(repo, ["hooks/sample.py"]) == ("selected", [["tests/test_sample.py"]])


def test_a_changed_test_file_selects_itself(repo):
    assert decide(repo, ["tests/test_other.py"]) == ("selected", [["tests/test_other.py"]])


def test_a_changed_file_outside_python_selects_the_tests_that_name_it(repo):
    write(repo, {"scripts/static/ledger.js": "x\n"})
    assert decide(repo, ["scripts/static/ledger.js"]) == ("selected", [["tests/test_page.py"]])


def test_a_changed_file_is_also_selected_by_its_full_path(repo):
    write(repo, {"tests/test_docs.py": "DOC = 'docs/guide/index.md'\n"})
    assert decide(repo, ["docs/guide/index.md"]) == ("selected", [["tests/test_docs.py"]])


def test_a_python_source_is_not_selected_by_its_bare_file_name(repo):
    write(repo, {"tests/test_names.py": "NAME = 'lonely.py'\n"})
    assert decide(repo, ["scripts/lonely.py"]) == ("full", [])


def test_a_deleted_test_file_is_not_selected(repo):
    assert decide(repo, ["tests/test_gone.py"]) == ("none", [])


def test_a_change_no_test_reaches_outside_the_sources_runs_nothing(repo):
    assert decide(repo, ["docs/unnamed.md"]) == ("none", [])


@pytest.mark.parametrize("name", ["hooks/new.py", "scripts/static/new.js"])
def test_a_covered_source_no_test_reaches_runs_the_full_suite(repo, name):
    write(repo, {name: "x = 1\n"})
    assert decide(repo, [name]) == ("full", [])


@pytest.mark.parametrize("name", ["hooks/new.css", "lib/new.py"])
def test_an_unreached_file_outside_the_covered_sources_runs_nothing(repo, name):
    write(repo, {name: "x\n"})
    assert decide(repo, [name]) == ("none", [])


def test_a_deleted_covered_source_no_test_names_runs_nothing(repo):
    assert decide(repo, ["hooks/removed.py"]) == ("none", [])


def seconds(**files):
    return {f"tests/{name}.py::test_case": value for name, value in files.items()}


@pytest.fixture
def heavy(repo):
    write(repo, {f"tests/test_heavy{index}.py": "from hooks import sample\n" for index in range(1, 4)})
    return repo


def test_a_selection_at_its_leg_budget_still_runs_selected(heavy):
    budget = ci_select.LEG_SECONDS
    durations = seconds(test_sample=0.5, test_heavy1=budget, test_heavy2=budget, test_heavy3=budget - 0.5)
    scope, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert scope == "selected"
    assert sorted(file for leg in legs for file in leg) == [
        "tests/test_heavy1.py",
        "tests/test_heavy2.py",
        "tests/test_heavy3.py",
        "tests/test_sample.py",
    ]
    assert len(legs) == 3


def test_a_selection_over_its_leg_budget_runs_the_full_suite(heavy):
    budget = ci_select.LEG_SECONDS
    durations = seconds(test_sample=1.0, test_heavy1=budget, test_heavy2=budget, test_heavy3=budget - 0.5)
    assert decide(heavy, ["hooks/sample.py"], durations) == ("full", [])


@pytest.mark.parametrize(("total", "count"), [(1.0, 1), (180.0, 1), (180.5, 2), (360.0, 2), (360.5, 3)])
def test_a_selection_uses_one_leg_per_leg_budget_of_stored_time(heavy, total, count):
    durations = seconds(test_sample=total / 4, test_heavy1=total / 4, test_heavy2=total / 4, test_heavy3=total / 4)
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert len(legs) == count


def test_a_test_file_without_stored_time_counts_its_estimate(heavy):
    durations = seconds(test_heavy1=ci_select.LEG_SECONDS - 3 * ci_select.UNKNOWN_SECONDS + 1)
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert len(legs) == 2


def test_the_legs_balance_on_stored_time(heavy):
    durations = seconds(test_sample=10.0, test_heavy1=170.0, test_heavy2=150.0, test_heavy3=20.0)
    assert decide(heavy, ["hooks/sample.py"], durations) == (
        "selected",
        [["tests/test_heavy1.py", "tests/test_sample.py"], ["tests/test_heavy2.py", "tests/test_heavy3.py"]],
    )


def test_the_live_codex_file_runs_on_the_first_leg(repo):
    write(
        repo,
        {
            "tests/routing/test_codex_api.py": "from hooks import sample\n",
            "tests/test_late.py": "from hooks import sample\n",
        },
    )
    durations = seconds(test_sample=170.0, test_late=170.0)
    _, legs = decide(repo, ["hooks/sample.py"], durations)
    assert len(legs) == 2
    assert legs[0][0] == "tests/routing/test_codex_api.py"
    assert sum("tests/routing/test_codex_api.py" in leg for leg in legs) == 1


def test_the_changed_files_come_from_the_merge_base(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "change")
    write(repo, {"hooks/sample.py": "VALUE = 3\n"})
    git(repo, "commit", "-am", "change")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "dev")
    write(repo, {"scripts/lonely.py": "VALUE = 4\n"})
    git(repo, "commit", "-am", "dev moved")
    assert ci_select.changed_files(repo, "dev", head) == ["hooks/sample.py"]
    assert ci_select.changed_files(repo, base, head) == ["hooks/sample.py"]


def run_main(repo, monkeypatch, tmp_path, event, base="HEAD~1"):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_select.main(["--base", base, "--head", "HEAD", "--event", event, "--legs", "3", "--root", str(repo)]) == 0
    return output.read_text()


def test_main_writes_the_selected_legs_for_a_pull_request(repo, monkeypatch, tmp_path, capsys):
    write(repo, {"hooks/sample.py": "VALUE = 5\n", ".test_durations": json.dumps(seconds(test_sample=2.0))})
    git(repo, "commit", "-am", "change")
    written = run_main(repo, monkeypatch, tmp_path, "pull_request")
    expected = "scope=selected\nlegs=[1]\ncount=1\nfiles_1=tests/test_sample.py\n"
    assert written == expected
    assert capsys.readouterr().out == expected


def test_main_writes_every_selected_leg(heavy, monkeypatch, tmp_path):
    durations = seconds(test_sample=170.0, test_heavy1=170.0, test_heavy2=170.0, test_heavy3=1.0)
    write(heavy, {"hooks/sample.py": "VALUE = 6\n", ".test_durations": json.dumps(durations)})
    git(heavy, "add", ".")
    git(heavy, "commit", "-m", "change")
    written = run_main(heavy, monkeypatch, tmp_path, "pull_request").splitlines()
    assert written[:3] == ["scope=selected", "legs=[1, 2, 3]", "count=3"]
    assert [line.partition("=")[0] for line in written[3:]] == ["files_1", "files_2", "files_3"]


@pytest.mark.parametrize("event", ["merge_group", "push", "workflow_dispatch"])
def test_main_runs_the_full_suite_outside_pull_requests(repo, monkeypatch, tmp_path, event):
    write(repo, {"hooks/sample.py": "VALUE = 7\n"})
    git(repo, "commit", "-am", "change")
    assert run_main(repo, monkeypatch, tmp_path, event, base="not-a-revision") == "scope=full\nlegs=[]\ncount=0\n"


def test_main_reports_a_change_that_selects_nothing(repo, monkeypatch, tmp_path):
    write(repo, {"docs/unnamed.md": "x\n"})
    git(repo, "add", ".")
    git(repo, "commit", "-m", "docs")
    assert run_main(repo, monkeypatch, tmp_path, "pull_request") == "scope=none\nlegs=[]\ncount=0\n"


def test_main_prints_without_a_github_output(repo, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert (
        ci_select.main(["--base", "HEAD", "--head", "HEAD", "--event", "push", "--legs", "3", "--root", str(repo)]) == 0
    )
    assert capsys.readouterr().out == "scope=full\nlegs=[]\ncount=0\n"
