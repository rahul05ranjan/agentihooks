#!/usr/bin/env bash
set -euo pipefail

# Times one new hive from its OpenBao invite to the first engineer its join report grades healthy.
# Run on the UI hive after the hive's GitOps values are synced into their own namespace.
label="${1:?usage: hive-reproduce.sh LABEL [NAMESPACE]}"
namespace="${2:-agentihooks-hive-$label}"
record="${HIVE_RECORD:-k8s/agentihooks-hive-$label}"
like="${HIVE_LIKE:-k8s/agentihooks-swarm-workstation-hive}"
step_timeout="${HIVE_STEP_TIMEOUT_S:-900}"
engineer_timeout="${HIVE_ENGINEER_TIMEOUT_S:-1800}"
out="${HIVE_TIMING_FILE:-}"
python="${HIVE_PYTHON:-python3}"

start=$(date +%s)
last=$start
timeline=()
mark() {
  local now
  now=$(date +%s)
  timeline+=("$(printf '%-22s %5ss  %5ss total  %s' "$1" "$((now - last))" "$((now - start))" "$2")")
  printf 'hive-reproduce: %s after %ss: %s\n' "$1" "$((now - start))" "$2"
  last=$now
}
report() {
  printf '\nstep                    took   elapsed\n'
  printf '%s\n' "${timeline[@]}"
  if [[ -n $out ]]; then
    printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$label" "$*" >> "$out"
    printf '%s\n' "${timeline[@]}" >> "$out"
  fi
}
fail() {
  mark "$1" "timed out: $2"
  report "failed at $1"
  exit 1
}
until_true() {
  local name="$1" limit="$2" detail="$3"
  shift 3
  local deadline=$(($(date +%s) + limit))
  until "$@"; do
    (($(date +%s) < deadline)) || fail "$name" "$detail"
    sleep 5
  done
}
probe() {
  "$python" - "$@" <<'PY'
import json
import sys
import time

import hooks.config  # noqa: F401
from scripts.hive import registry
from scripts.swarm.store import RedisStore, redis_client

what, member = sys.argv[1:3]
redis = redis_client()
if what == "live":
    record = registry.show(redis, member)
    sys.exit(0 if record and registry.live(record, time.time_ns() // 1_000_000) else 1)
if what == "engineer":
    store = RedisStore(redis)
    for slug in store.slugs():
        for agent in store.agents(slug):
            if agent.hive == member and agent.lane != "master" and agent.state == "working":
                print(f"{agent.name} in {slug}")
                sys.exit(0)
    sys.exit(1)
for raw in redis.hvals(f"{registry.key(member)}:join-reports"):
    found = json.loads(raw)
    if found.get("grade") in ("complete", "partial"):
        print(f"{found['agent']} graded {found['grade']}")
        sys.exit(0)
sys.exit(1)
PY
}
synced() {
  [[ -z $(kubectl -n "$namespace" get externalsecret \
    -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -v '^True$') ]]
}
pod_ready() {
  kubectl -n "$namespace" wait pod -l app.kubernetes.io/component=worker-hive --for=condition=Ready --timeout=5s \
    >/dev/null 2>&1
}

kubectl get namespace "$namespace" >/dev/null
mark "namespace" "$namespace exists"

invited=$(agentihooks hive invite "$label" --path "$record" --like "$like")
member=$(sed -n 's/^invited hive member \([0-9a-f]*\) .*/\1/p' <<<"$invited")
[[ -n $member ]] || { echo "hive-reproduce: hive invite printed no member: $invited" >&2; exit 1; }
mark "invite" "$invited"

kubectl -n "$namespace" annotate externalsecret --all "force-sync=$(date +%s)" --overwrite >/dev/null
until_true "secrets synced" "$step_timeout" "an ExternalSecret in $namespace is not Ready" synced
mark "secrets synced" "every ExternalSecret in $namespace is Ready"

kubectl -n "$namespace" rollout restart deployment -l app.kubernetes.io/component=worker-hive >/dev/null
until_true "pod ready" "$step_timeout" "the worker hive pod in $namespace is not Ready" pod_ready
mark "pod ready" "the worker hive pod is Ready"

until_true "heartbeat" "$step_timeout" "hive $member has no live heartbeat" probe live "$member"
mark "heartbeat" "hive $member is live"

until_true "engineer working" "$engineer_timeout" "no engineer works on hive $member" probe engineer "$member"
mark "engineer working" "$(probe engineer "$member")"

until_true "join report" "$step_timeout" "no join report on hive $member grades complete or partial" \
  probe report "$member"
mark "join report" "$(probe report "$member")"

report "healthy engineer after $(($(date +%s) - start))s"
