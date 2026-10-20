"""agentihooks hive add: a UI hive saves a machine in herdr and delivers its member credential over SSH."""

import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.hive import auth, registry
from scripts.hive.placement import YES

if TYPE_CHECKING:
    from redis import Redis

REMOTE_SESSION = "default"
HERDR_TIMEOUT_S = 300
SSH_TIMEOUT_S = 60
# The remote shell resolves its own home; a worker hive's sshd sets AGENTIHOOKS_HOME.
DELIVER = (
    'umask 077 && home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}" && mkdir -p "$home" '
    '&& cat > "$home/hive.env.new" && mv "$home/hive.env.new" "$home/hive.env"'
)
DELIVER_CA = (
    'home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}" && ca="${LEDGER_CA_FILE:-$home/ledger-ca.pem}" '
    '&& mkdir -p "$(dirname "$ca")" && cat > "$ca.new" && mv "$ca.new" "$ca"'
)


def add(
    redis: "Redis",
    local_id: str,
    target: str,
    *,
    label: str,
    url: str,
    redis_url: str,
    ledger_ca: str | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    local = registry.show(redis, local_id)
    if local is None or local["ui"] != YES:
        raise auth.HiveError(f"hive {local_id} is headless; only a UI hive adds machines")
    _run(
        run,
        ["herdr", "machine", "add", "--label", label, "--remote-session", REMOTE_SESSION, target],
        None,
        HERDR_TIMEOUT_S,
    )
    grant = auth.mint(redis, label, redis_url)
    try:
        _run(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER], auth.env_text(url, grant), SSH_TIMEOUT_S)
        if ledger_ca:
            _run(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER_CA], ledger_ca, SSH_TIMEOUT_S)
    except auth.HiveError:
        auth.revoke(redis, grant["id"])
        raise
    return grant


def _run(run: Callable[..., subprocess.CompletedProcess], argv: list[str], stdin: str | None, timeout: int) -> None:
    try:
        done = run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise auth.HiveError(f"{argv[0]} failed ({exc})") from exc
    if done.returncode != 0:
        raise auth.HiveError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()}")
