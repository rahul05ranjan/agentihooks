"""Codex hook trust hashes, computed the way Codex's hook discovery computes them.

Codex runs a hooks.json entry only when `hooks.state."<hooks path>:<event>:<group>:<handler>".trusted_hash`
equals the sha256 of the handler's normalized identity, serialized as JSON with sorted keys.
"""

import hashlib
import json
from pathlib import Path

EVENT_LABELS = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "Interrupt": "interrupt",
}
UNMATCHED_EVENTS = frozenset({"UserPromptSubmit", "Stop", "Interrupt"})
CONTEXT_EVENTS = frozenset({"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart"})
SHORT_EVENTS = frozenset({"SessionEnd", "Interrupt"})
DEFAULT_CONTEXT_LIMIT = 2500


def _timeout(event: str, value: int | None) -> int:
    if event in SHORT_EVENTS:
        return min(max(1 if value is None else value, 1), 3)
    return max(600 if value is None else value, 1)


def hook_hash(event: str, matcher: str | None, hook: dict) -> str:
    handler = {
        "type": "command",
        "command": hook["command"],
        "timeout": _timeout(event, hook.get("timeout")),
        "async": bool(hook.get("async", False)),
    }
    if hook.get("statusMessage") is not None:
        handler["statusMessage"] = hook["statusMessage"]
    limit = hook.get("additionalContextLimit") if event in CONTEXT_EVENTS else None
    if limit is not None and limit != DEFAULT_CONTEXT_LIMIT:
        handler["additionalContextLimit"] = limit
    identity = {"event_name": EVENT_LABELS[event], "hooks": [handler]}
    if matcher is not None and event not in UNMATCHED_EVENTS:
        identity["matcher"] = matcher
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def trusted_state(hooks: Path, key_path: Path) -> dict[str, dict[str, str]]:
    try:
        doc = json.loads(hooks.read_text())
    except (OSError, ValueError):
        return {}
    events = doc.get("hooks") if isinstance(doc, dict) else None
    state = {}
    for event, groups in (events if isinstance(events, dict) else {}).items():
        if event not in EVENT_LABELS or not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            for handler_index, hook in enumerate(group.get("hooks") or []):
                if hook.get("type") != "command" or not str(hook.get("command", "")).strip():
                    continue
                key = f"{key_path}:{EVENT_LABELS[event]}:{group_index}:{handler_index}"
                state[key] = {"trusted_hash": hook_hash(event, group.get("matcher"), hook)}
    return state
