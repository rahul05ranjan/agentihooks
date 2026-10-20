"""agentihooks hive bundle-origin: the linked bundle's git address and branch into the hive settings record."""

import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from scripts import openbao_kv
from scripts.hive import auth, registry

SETTINGS_PATH = "k8s/agentihooks-swarm-workstation-hive-settings"
URL_KEY = "HIVE_BUNDLE_URL"
REF_KEY = "HIVE_BUNDLE_REF"


def origin(home: Path) -> dict[str, str]:
    from scripts.hive.daemon import origin as remote

    try:
        bundle = (json.loads((home / "state.json").read_text()).get("bundle") or {}).get("path")
    except (OSError, ValueError, AttributeError):
        bundle = None
    if not bundle:
        return {URL_KEY: "", REF_KEY: ""}
    identity = remote(Path(bundle))
    if not identity:
        raise auth.HiveError(f"the linked bundle at {bundle} has no origin remote naming a host and repository")
    branch = subprocess.run(
        ["git", "-C", bundle, "branch", "--show-current"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not branch:
        raise auth.HiveError(f"the linked bundle at {bundle} is not on a branch")
    return {URL_KEY: f"https://{identity}.git", REF_KEY: branch}


def publish(environ: Mapping[str, str], path: str, opener: Callable | None = None) -> str:
    bao = openbao_kv.from_env(environ, opener)
    if bao is None:
        raise auth.HiveError(f"{openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must both be set")
    entries = origin(registry.home(environ))
    try:
        total = openbao_kv.merge(bao, path, entries)
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    linked = f"bundle {entries[URL_KEY]} at {entries[REF_KEY]}" if entries[URL_KEY] else "no bundle is linked"
    return f"{openbao_kv.summary(path, entries, total)}; {linked}"
