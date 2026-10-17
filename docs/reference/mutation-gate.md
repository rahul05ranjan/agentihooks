# Pull request mutation gate

The Mutation workflow runs beside Tests for pull requests into dev. It discovers changed Python files in `hooks` and `scripts` from the pull request merge base. Each file runs in an isolated copy with pinned mutmut 3.6.0. Pytest runs serially; matching and importing test modules provide mutmut's function coverage stats and per mutant test selection.

Run locally with the workspace Python:

```bash
~/dev/tcc-ecosystem/.venv/bin/python -m scripts.ci_mutation --base origin/dev
```

## Graded lines

A branch is graded only on its own lines, so a dev refresh or a stacked branch does not pay for work that was already graded:

- **Real bases.** The plan job resolves them once:
  - the merge base with the given base;
  - the merge base with `origin/dev`;
  - every pushed commit inside the head whose own push Mutation preflight had a successful `mutation` job.
- **Which lines count.** A line is graded only when it differs from every real base. Because the given base always counts, the scope never grows past `base...head`.
- **Proof of grading.** The plan reads the preflight proof through the Actions API on the App token. Dispatched runs and pull request gates never count, since they choose their own base or may skip mutation.
- **Weakened tests.** The narrowing never hides weakened tests. If the head changed `tests/` against a newer base beyond adding tests, every line that base excluded is graded again. Adding tests means new `test_` functions, or new `Test` classes holding only test methods, with only `pytest.mark` decorators, or imports in a new test file, and no reused or repeated name. An import added to an existing test file counts as a change, since it can shadow a fixture or builtin those tests use. Anything else counts as a change: an edit, a deletion, a rename, a skip marker, a fixture, a helper, a conftest or a data file.
- **One list for every job.** The plan writes the resolved list as `bases`. The stats parts, shards, push preflight and the proofs mutation take it with `--bases`, so every job grades the same mutants.

- **Reuse on the push preflight.** Before the mutation job takes a mutation lane, which the proofs workflow and other dispatches never wait on, the cheap decision job reads the newest graded push preflight for the same head on any branch, then the branch's newest. When that run concluded success with a complete green report and this head equals it or only adds tests since, as defined above, its report is uploaded as this run's `mutation-preflight-report` with a `reuse.json` naming the source run, and the mutation job is skipped. A newer red or failed run, a missing report, any change outside `tests/` or any lookup error runs the mutation job as before, so no head goes green without a complete green report. A reused head never counts as a graded base. Measured on PR 2624: the full preflight 38151448161 took 116 s; the tests only head after it reused that report in run 38151636224, whose decision job took 18 s.

## Touched tests on the push preflight

A branch that adds only failing tests has no mutants, and a head that only adds tests may reuse an earlier green report, so the mutation path alone stays green on the red step of red then green. The `touched-tests` job closes that gap: on every branch push it runs the test files the branch touches and fails the Mutation preflight run when any of them fails.

- **When it runs.** On branch pushes the Mutation preflight takes, which excludes `dev`, `main`, `gh-readonly-queue/**`, `wip/**` and `diffcheck/**`. It needs the `pull-request` decision job and runs only while the branch has no open pull request into dev, the same condition that lets the mutation path run. It runs beside `plan` and `mutation`, never after them, and runs whether or not the report was reused, so a green preflight takes no longer. Dispatched runs (`workflow_call`) skip it.
- **Which tests.** Files that `git diff --name-only --no-renames origin/dev...HEAD` names, that sit under `tests/`, are named `test_*.py` and still exist at the head. A deleted test file is left out; a renamed one counts as its new name. Fixtures, conftest files and helpers do not select tests on their own. With no touched test the run step is skipped and the job passes. The job log prints the list under `Touched tests:`.
- **Browser.** When a touched test, or a `conftest.py` or `__init__.py` above it, drives Playwright, the job installs Chromium through the shared `browser-cache` action: it uses the runner image's browser or a verified cached one, else installs with one retry, inside a six minute step timeout. Otherwise no browser is installed.
- **How it runs.** Python 3.12, the `dev` and `all` extras without the test excludes, then `python -m pytest <files> -n auto --tb=short -p no:cacheprovider`. A failing test is never retried and no step continues on error, so one failure turns the job and the run red. The job runs on the `anton-oss` pool for trusted actors, otherwise `ubuntu-latest`, with a fifteen minute timeout.

Read a branch push's result:

```bash
gh run list --workflow mutation-preflight.yml --branch <branch> --limit 1 --json databaseId,conclusion,headSha
gh api repos/the-cloud-clockwork/agentihooks/actions/runs/<run id>/jobs \
  --jq '.jobs[] | "\(.name) \(.conclusion)"'
```

A red `touched-tests` with green `mutation` means a touched test failed; the pytest summary in the job log names it. Swarm readers grade the run conclusion, so that red reaches them as a red preflight.

Measured on the pull request that delivered the job, #3959:

| Commit | Workflow | Preflight | `touched-tests` | Run |
|---|---|---|---|---|
| Adds a failing test, job absent | old | success | absent | 39018334066 |
| Adds the job and one planted failing assertion | new | failure | failure in 40 s: `assert 15 == 1`, 1 failed, 83 passed | 39018525928 |
| Fix commit | new | success | success in 35 s | 39018719387 |
| Final head after review | new | success | success in 39 s | 39019128351 |

In every run the `mutation` job took 18 s to 20 s, so the job ran inside the time the mutation path already spends.

Limits and troubleshooting:

- Once the branch has an open pull request into dev the push preflight skips every job, this one included; the pull request's own Tests run grades the tests from then on. Read red before opening the first draft.
- Only touched test files run. A source change that breaks an untouched test turns red in the pull request's Tests run, not here.
- A test that passes alone but fails beside others under `-n auto` fails here too; fix the test's isolation, since no retry absorbs it.
- A red `touched-tests` whose failed step is the browser install, not the pytest step, comes from the runner, not a test; the step log names the failed install attempts.

Measured on dispatched proofs:

| Case | Old scope | New scope | Shard time |
|---|---|---|---|
| Branch refreshed with dev, base pinned to an older dev commit | 17 files | 203 own mutants | 39 s |
| Branch stacked on a graded branch | earlier branch's 303 mutants plus own | 13 own mutants | 23 s |

On the pull request that delivered this, the whole mutation chain finished 184 s into the Tests run: the plan, including the App token mint, took 21 s, and the stats and two shards finished by 184 s. Gate Required finished at 431 s, well inside the fifteen minute budget. Sonar and the coverage ratchet set that time, not mutation.

`--head`, `--output` and `--budget` override the comparison head, evidence directory and total seconds. The default budget is eighteen minutes, leaving eight minutes for setup, both browser install attempts and evidence upload in the twenty six minute job. Over budget files and failed runs are named and fail the gate. All outcomes, including survivors on untouched lines, are recorded in `report.json`; CI uploads the complete evidence directory on success or failure.

Mutmut 3 mutates functions and methods. Files without mutable functions are reported explicitly with a zero count. A surviving or uncovered mutant fails when its original source lines intersect added or changed hunk lines. Incomplete, timed out and unexpected mutant outcomes fail regardless of their line. Generated trampolines and function relative diffs are mapped back to the original source, including decorators and methods.

A Standards reader clears an exact survivor with one JSON file under `mutation-clearances/`. Each file holds one key combining the source file, mutant name and fingerprint from the report. The filename is the SHA256 of that complete key followed by `.json`, so different rulings can be added by concurrent pull requests without editing a shared object. Every entry requires the reader and its reason. The runner also reads the legacy root `mutation-cleared.txt`; identical duplicates are accepted and conflicting rulings fail. The fingerprint binds the original function and mutant, so a changed function invalidates an old clearance.

```json
{
  "hooks/example.py:hooks.example.x_example__mutmut_1:FINGERPRINT": {
    "reader": "Standards",
    "reason": "The changed argument does not change the observed output."
  }
}
```

Write an independently reviewed ruling with `scripts.ci_mutation.clearances.write_clearance(root, key, ruling)`; `clearance_path(root, key)` returns its deterministic filename. Run `python -m scripts.ci_mutation.clearances` from the repository root to migrate legacy rulings without changing their reader or reason. The migration leaves an empty legacy object for compatible older readers. Trace plans and the build scope gate accept both clearance locations as supporting proof.
