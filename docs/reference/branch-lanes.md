---
title: Branch workflow lanes
parent: Reference
nav_order: 10
---

# Branch workflow lanes

The `Branch workflow lanes` workflow grades every branch push for workflow files whose jobs would run outside the shared hosted lanes. A workflow that exists only on a branch cannot be capped from `dev`, so the grader refuses it, cancels its runs on that head and fails.

## When it runs

The workflow runs on every push to a branch other than `dev`, `main` and the merge queue's `gh-readonly-queue/**` branches, proof and diffcheck branches included. It skips branch deletions. Its single job, `grade`, checks out `dev` and runs `python -m scripts.ci_branch_lanes --head <pushed sha> --repo <repository>`, so the grader that judges a head is always the one on `dev`. A change to the grader takes effect on branch pushes only after it merges into `dev`.

## What it grades

The grader reads every `.yml` and `.yaml` file under `.github/workflows` on the pushed head and grades two kinds:

- a workflow file that does not exist on `dev`;
- a workflow file whose text differs from the `dev` copy.

Each job in a graded file must use, at job level or inherited from the workflow level, a concurrency block that:

- names the shared hosted lane group exactly;
- sets `queue: max`;
- leaves `cancel-in-progress` unset or `false`.

```yaml
concurrency:
  group: ${{ format('hosted-lane-{0}', ((endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || endsWith(github.run_id, '2')) && 'a') || ((endsWith(github.run_id, '3') || endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b') || 'c') }}
  queue: max
```

In an edited workflow whose `name` matches the `dev` copy, a job whose effective concurrency block equals the one `dev` gives it passes unchanged, so step edits to existing workflows are not refused. A job whose block changed, or a new job that `dev` gives no block, is graded like a new one. A renamed workflow is graded in full.

A refused head prints one line per problem and the step fails:

```text
Refused .github/workflows/probe.yml: job probe has no shared hosted lane concurrency group.
Refused .github/workflows/probe.yml: job probe queues on the lane without queue: max.
Refused .github/workflows/probe.yml: job probe may cancel other runs on the shared lane.
Refused .github/workflows/probe.yml: does not parse as YAML.
```

A head with nothing to refuse prints `Every workflow that exists only on this head queues on a shared hosted lane.` and passes.

## Cancelling the refused runs

On a refused head the grader cancels the runs of each refused workflow on that head, then exits 1. It lists the head's workflow runs every 5 seconds and asks GitHub to cancel each matching run that has not completed. It prints `Cancelled run <id>.` for every cancel GitHub accepted.

The wait ends as soon as both hold on one listing:

- every refused workflow has at least one run on the head, whatever its status;
- GitHub accepted a cancel for every matching run that was still live.

A head whose matching runs have all completed therefore ends the wait on the first listing, with nothing to cancel. The wait keeps polling while a refused workflow has no run yet, or while a live run's cancel was refused; a refused cancel is retried on the next listing. An accepted cancel ends the wait even while GitHub still shows that run as running.

The wait gives up after 60 seconds, the `--wait` default, and returns whatever it cancelled. A refused workflow that never starts a run on a push, such as one triggered only by `workflow_dispatch`, holds the wait to that deadline.

Before this rule, a workflow counted as seen only while one of its runs was running, so a refused head whose matching runs had already completed waited the full 60 seconds. Refused heads spent 62 seconds in the grader step on runs [38999824972](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38999824972) and [38998263485](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/38998263485).

## Running it locally

Grading needs only git and the checkout to compare against. Without `--repo` the grader refuses and prints reasons but cancels nothing:

```bash
git fetch origin <branch>
python -m scripts.ci_branch_lanes \
  --head origin/<branch>
```

With `--repo` it also cancels the refused runs through `gh`, so the `gh` login needs `actions: write` on the repository:

```bash
python -m scripts.ci_branch_lanes \
  --head <sha> \
  --repo the-cloud-clockwork/agentihooks \
  --wait 60
```

Run it from a `dev` checkout: the working tree is the baseline that decides which workflows are new or edited.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `has no shared hosted lane concurrency group` | The job, and its workflow, set no concurrency or a different group | Copy the group expression above exactly; extra spaces and line breaks pass, any other change does not |
| `queues on the lane without queue: max` | The block names the lane but omits `queue: max` | Add `queue: max` |
| `may cancel other runs on the shared lane` | `cancel-in-progress` is `true` or an expression | Remove it or set `false` |
| The grader step takes about 60 seconds | A refused workflow never started a run on that head | Expected; it ends at the deadline. Fix the workflow so the next push passes |
| An edit to an existing workflow is refused | The edit changed a job's concurrency block, added a job without one, or renamed the workflow | Keep the `dev` concurrency block and `name`, or give the job the lane block |
