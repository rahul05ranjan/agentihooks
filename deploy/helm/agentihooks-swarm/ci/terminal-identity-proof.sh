#!/usr/bin/env bash
set -euo pipefail

release=$1
slug=$2
execution=$3
agent=$4
controller=("deployment/$release-controller" -c controller)

master="$(kubectl exec "${controller[@]}" -- env SLUG="$slug" python -c '
import os
from scripts.swarm.store import AgentRecord, connect
store, slug = connect(), os.environ["SLUG"]
name = store.next_name(slug, "master")
store.put_agent(slug, AgentRecord(name, "master", "master", seat=f"master@{slug}"))
print(name)' | tail -1)"
printf 'seated %s as the master of %s\n' "$master" "$slug"

run="$(cat <<'EOF'
import os, sys

from scripts.swarm import cli
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.operator_auth import OPERATOR

if os.environ.get("PLANT") == "every caller is the operator":
    operator_terminal.principal = lambda *_: OPERATOR
sys.exit(cli.main([sys.argv[1], "terminal", sys.argv[2]]))
EOF
)"

terminal() {
  local fault=$1
  shift
  kubectl exec "${controller[@]}" -- env -u AGENTIHOOKS_AGENT_NAME -u AGENTIHOOKS_SWARM "$@" PLANT="$fault" \
    python -c "$run" "$slug" "$execution" 2>&1
}

check() {
  local fault=$1 out
  if ! out="$(terminal "$fault")"; then
    printf 'the operator identity could not attach to the worker terminal %s: %s\n' "$execution" "$out"
    return 1
  fi
  if [[ $out != *"\"incarnation\": \"$execution/"* ]]; then
    printf 'the operator identity attach named no incarnation of %s: %s\n' "$execution" "$out"
    return 1
  fi
  printf 'the operator identity attached to the worker terminal: %s\n' "$(tail -1 <<< "$out")"
  if out="$(terminal "$fault" AGENTIHOOKS_AGENT_NAME="$master" AGENTIHOOKS_SWARM="$slug")"; then
    printf 'the master identity %s was not refused the worker terminal: %s\n' "$master" "$out"
    return 1
  fi
  if [[ $out != *"only the operator role opens a worker terminal"* ]]; then
    printf 'the master identity %s was refused for another reason: %s\n' "$master" "$out"
    return 1
  fi
  printf 'the master identity was refused: %s\n' "$(tail -1 <<< "$out")"
  if out="$(terminal "$fault" AGENTIHOOKS_AGENT_NAME="$agent" AGENTIHOOKS_SWARM="$slug")"; then
    printf 'the agent identity %s was not refused the worker terminal: %s\n' "$agent" "$out"
    return 1
  fi
  if [[ $out != *"only the operator or the master of swarm $slug uses its swarm controls"* ]]; then
    printf 'the agent identity %s was refused for another reason: %s\n' "$agent" "$out"
    return 1
  fi
  printf 'the agent identity was refused: %s\n' "$(tail -1 <<< "$out")"
}

if ! check ""; then
  exit 1
fi
if planted="$(check "every caller is the operator")"; then
  printf 'the identity check passed with every caller resolved to the operator: %s\n' "$planted" >&2
  exit 1
fi
if [[ $planted != *"the master identity $master was not refused the worker terminal"* ]]; then
  printf 'the identity check missed the planted operator fallback for the master: %s\n' "$planted" >&2
  exit 1
fi
printf 'the identity check flagged the planted operator fallback: %s\n' "$(tail -1 <<< "$planted")"
