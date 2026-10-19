#!/usr/bin/env bash
set -euo pipefail
R=$GITHUB_REPOSITORY
MODE=$(cat .github/lost-runner-mode)
watchdog() { python -m scripts.ci_lost_runner --repo "$R" --workflow lost-runner-plant.yml --grace-minutes 0.5 "$@"; }
state() { gh api "repos/$R/actions/runs/$PLANT" --jq '"\(.run_attempt) \(.status) \(.conclusion)"'; }
lane() { case "${1: -1}" in 0|1|2) echo a ;; 3|4|5) echo b ;; *) echo c ;; esac; }
MINE=$(lane "$GITHUB_RUN_ID")
BRANCH="${GITHUB_REF_NAME}-plant-${GITHUB_RUN_ID}"
git config user.name "lost-runner-proof"
git config user.email "lost-runner-proof@users.noreply.github.com"
trap 'git push -q "https://x-access-token:${GH_TOKEN}@github.com/${R}.git" --delete "$BRANCH" || true' EXIT
PLANT=""
for try in $(seq 8); do
  git commit -q --allow-empty -m "Plant a lost runner, try $try"
  git push -q "https://x-access-token:${GH_TOKEN}@github.com/${R}.git" "HEAD:refs/heads/$BRANCH"
  SHA=$(git rev-parse HEAD)
  PLANT=""
  for _ in $(seq 30); do
    PLANT=$(gh api "repos/$R/actions/workflows/lost-runner-plant.yml/runs?head_sha=$SHA" --jq '.workflow_runs[0].id // empty')
    [[ -n "$PLANT" ]] && break
    sleep 5
  done
  [[ -n "$PLANT" ]] || { echo "::error::no plant run for $SHA"; exit 1; }
  echo "try $try: plant run $PLANT on lane $(lane "$PLANT"), proof on lane $MINE"
  [[ "$(lane "$PLANT")" != "$MINE" ]] && break
  gh api -X POST "repos/$R/actions/runs/$PLANT/cancel" >/dev/null
  PLANT=""
done
[[ -n "$PLANT" ]] || { echo "::error::every plant landed on the proof lane"; exit 1; }
echo "mode $MODE, plant run $PLANT"
until_acted() {
  local want=$1 line action
  for _ in $(seq 160); do
    line=$(watchdog --run-id "$PLANT")
    echo "$(date -u +%T) $line"
    for name in $(gh api "repos/$R/actions/runs/$PLANT/jobs?filter=latest" --jq '.jobs[] | select(.status == "in_progress") | .runner_name'); do
      echo "  runner $name: $(gh api --paginate "orgs/${R%%/*}/actions/runners?per_page=100" --jq ".runners[] | select(.name == \"$name\") | \"\(.status) busy=\(.busy)\"")"
    done
    action=$(jq -r .action <<<"$line")
    if [[ "$action" != none ]]; then
      [[ "$action" == "$want" ]] || { echo "::error::expected $want, got $action"; exit 1; }
      return 0
    fi
    [[ "$(jq -r .reason <<<"$line")" != "run completed"* ]] || { echo "::error::run ended without the watchdog"; exit 1; }
    sleep 15
  done
  echo "::error::run $PLANT never stalled"
  exit 1
}
if [[ "$MODE" == plant ]]; then
  until_acted rerun
  for _ in $(seq 30); do [[ "$(state)" == 2\ * ]] && break; sleep 10; done
  echo "after rerun: $(state)"
  [[ "$(state)" == 2\ * ]] || { echo "::error::the rerun did not start"; exit 1; }
  until_acted cancel
  sleep 60
  final=$(state)
  echo "final: $final"
  [[ "$final" == "2 completed cancelled" ]] || { echo "::error::expected attempt 2 cancelled, got $final"; exit 1; }
  gh api "repos/$R/actions/runs/$PLANT/attempts/2/jobs" --jq '.jobs[] | "attempt 2 job \(.name): \(.conclusion)"'
else
  until_acted cancel
  sleep 60
  final=$(state)
  echo "final: $final"
  [[ "$final" == "1 completed cancelled" ]] || { echo "::error::expected attempt 1 cancelled and never rerun, got $final"; exit 1; }
  line=$(watchdog --run-id "$RED_RUN" --workflow test.yml)
  echo "real failure run: $line"
  [[ "$(jq -r .action <<<"$line")" == none ]] || { echo "::error::the real failure run was touched"; exit 1; }
fi
echo "proof passed"
