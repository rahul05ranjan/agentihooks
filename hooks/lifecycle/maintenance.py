from pathlib import Path

from hooks.lifecycle.config import load_roots
from hooks.lifecycle.gitstate import git
from hooks.lifecycle.lease import admin_dir
from hooks.lifecycle.model import Root
from hooks.lifecycle.timer import _live_home
from hooks.lifecycle.worktrees import discover


def primaries(roots: list[Root]) -> list[Path]:
    return sorted({admin.parent.parent.parent for path, _root in discover(roots) if (admin := admin_dir(path))})


def registered_repos() -> list[str]:
    result = git(Path.home(), "config", "--global", "--get-all", "maintenance.repo")
    return [str(Path(line).resolve()) for line in result.stdout.splitlines() if line]


def objects(primary: Path) -> dict:
    result = git(primary, "count-objects", "-v")
    counts = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    return {"loose": int(counts.get("count", 0)), "packs": int(counts.get("packs", 0))}


def register(primary: Path) -> str:
    if _live_home() and git(primary, "maintenance", "start").returncode == 0:
        return "registered"
    result = git(primary, "maintenance", "register")
    return "registered" if result.returncode == 0 else f"failed: {result.stderr.strip()}"


def maintain(paths: list[Path], act: bool) -> list[dict]:
    known, report = set(registered_repos()), []
    for primary in paths:
        registered, outcome = str(primary.resolve()) in known, ""
        if act and not registered:
            outcome = register(primary)
            registered = outcome == "registered"
        report.append({"path": str(primary), "registered": registered, "outcome": outcome, **objects(primary)})
    return report


def setup(roots: list[Root] | None = None) -> list[str]:
    report = maintain(primaries(load_roots() if roots is None else roots), act=True)
    return [
        f"[{'OK' if item['registered'] else '!!'}] git maintenance {item['outcome']} for {item['path']}"
        for item in report
        if item["outcome"]
    ]
