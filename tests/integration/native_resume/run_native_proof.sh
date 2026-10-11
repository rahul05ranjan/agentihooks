#!/usr/bin/env bash
set -euo pipefail

# Live SV2-CKP-03 proof for one installed CLI: a real conversation is told a codeword in a source home, carried through
# a granted checkpoint store into a fresh home under another working directory, and resumed there. The fresh home gets
# the account only through the environment, never through a file in the carried material.
# Claude takes its account from the variable named by NATIVE_PROOF_CLAUDE_ACCOUNT (an AH_CC_TOKEN_<slug>);
# Codex is routed by agentihooks codex to the account named by NATIVE_PROOF_CODEX_ROUTE, because the fresh home holds
# no quota readings the router could place by.

proof_root="${1:?work directory required}"
harness="${2:?harness required: claude or codex}"
proof_python="${NATIVE_PROOF_PYTHON:-python3}"
proof_repo="$(git rev-parse --show-toplevel)"
codeword="PLUM-$(python3 -c 'import secrets; print(secrets.randbelow(9000) + 1000)')"

mkdir -p "$proof_root"/{source-home,source-cwd,fresh-home,fresh-cwd,durable}
proof_root="$(cd "$proof_root" && pwd)"
results="$proof_root/results.jsonl"
: > "$results"

record() {
    python3 -c 'import json, sys; print(json.dumps({"step": sys.argv[1], "exit": int(sys.argv[2]), "output": sys.argv[3]}))' \
        "$1" "$2" "$3" >> "$results"
}

run_cli() {
    local home="$1" cwd="$2"
    shift 2
    if [[ "$harness" == "claude" ]]; then
        local account="${NATIVE_PROOF_CLAUDE_ACCOUNT:?name the AH_CC_TOKEN_<slug> variable to use}"
        (cd "$cwd" && CLAUDE_CONFIG_DIR="$home" CLAUDE_CODE_OAUTH_TOKEN="${!account}" timeout 300 claude -p "$@")
    else
        local route="${NATIVE_PROOF_CODEX_ROUTE:?name the Codex account slug to route}"
        (cd "$cwd" && CODEX_HOME="$home" timeout 300 agentihooks codex --route "$route" exec --skip-git-repo-check "$@")
    fi
}

version="$("$harness" --version 2>&1 | head -1)"
record installed_version 0 "$version"

set +e
told="$(run_cli "$proof_root/source-home" "$proof_root/source-cwd" "Remember the codeword $codeword. Reply only: ok" 2>&1)"
told_exit=$?
set -e
record source_session "$told_exit" "$(tail -c 400 <<< "$told")"
[[ "$told_exit" -eq 0 ]] || { echo "the source conversation did not start" >&2; exit 1; }

carried="$(cd "$proof_repo" && "$proof_python" -m tests.integration.native_resume.native_proof "$harness" \
    "$proof_root/source-home" "$proof_root/source-cwd" "$proof_root/fresh-home" "$proof_root/fresh-cwd" \
    "$proof_root/durable" "$version")"
record carry 0 "$carried"
session="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["session_id"])' "$carried")"
outcome="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["restore"]["outcome"])' "$carried")"
[[ "$outcome" == "resumed" ]] || { echo "restore did not qualify: $outcome" >&2; exit 1; }

question="What was the codeword I asked you to remember? Reply with the codeword only."
set +e
if [[ "$harness" == "claude" ]]; then
    answer="$(run_cli "$proof_root/fresh-home" "$proof_root/fresh-cwd" --resume "$session" "$question" 2>&1)"
else
    answer="$(run_cli "$proof_root/fresh-home" "$proof_root/fresh-cwd" resume "$session" "$question" 2>&1)"
fi
answer_exit=$?
set -e
record fresh_home_resume "$answer_exit" "$(tail -c 400 <<< "$answer")"
grep -q "$codeword" <<< "$answer" && recalled=0 || recalled=1
record codeword_recalled "$recalled" "expected $codeword in the resumed answer"
exit "$recalled"
