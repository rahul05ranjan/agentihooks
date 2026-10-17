"""The Claude login only master profile homes link: one credentials file under the agentihooks home."""

from collections.abc import Sequence
from pathlib import Path

CREDENTIALS = ".credentials.json"
FOLDER = "master-login"


def directory() -> Path:
    from scripts.targets._common import _install_module

    return _install_module().AGENTIHOOKS_STATE_DIR / FOLDER


def credentials() -> Path:
    return directory() / CREDENTIALS


def is_master(chain: Sequence[str]) -> bool:
    return any(name.removeprefix("package:") == "master" for name in chain)


def link(home: Path, chain: Sequence[str]) -> None:
    if is_master(chain):
        directory().mkdir(mode=0o700, parents=True, exist_ok=True)
        (home / CREDENTIALS).symlink_to(credentials())


def fresh(home: Path, chain: Sequence[str]) -> bool:
    path = home / CREDENTIALS
    if is_master(chain):
        return path.is_symlink() and path.readlink() == credentials()
    return not path.is_symlink() and not path.exists()
