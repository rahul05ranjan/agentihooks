from pathlib import Path

REPO = Path(__file__).resolve().parent


def _account_home() -> Path:
    return Path.home()


def _live(home: Path) -> bool:
    return False


def _systemctl(*args: str) -> bool:
    return True


def drift(home: Path) -> list[str]:
    return []


def install(home: Path) -> list[str]:
    return []


def reconcile(home: Path) -> str:
    return ""


def main(argv: list[str]) -> int:
    return 0
