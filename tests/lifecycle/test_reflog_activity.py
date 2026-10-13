import os

from hooks.lifecycle.lease import admin_dir
from hooks.lifecycle.worktrees import classify

from .conftest import age, git, snap


def test_reflog_expiry_does_not_renew_idle_worktree(repo):
    path = repo.worktree("expired-log")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    old_content = log.read_bytes()
    old_mtime = log.stat().st_mtime

    git(path, "reflog", "expire", "--all", "--expire=never", "--expire-unreachable=never")

    assert log.read_bytes() == old_content
    assert log.stat().st_mtime > old_mtime
    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("remove", "clean and on a remote")


def test_recent_reflog_event_keeps_worktree_with_old_file_mtimes(repo):
    path = repo.worktree("recent-event")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    old_mtime = log.stat().st_mtime
    header, message = log.read_text().splitlines()[-1].split("\t", 1)
    identity, _, zone = header.rsplit(" ", 2)
    log.write_text(f"{identity} {int(snap().now) - 60} {zone}\t{message}\n")
    os.utime(log, (old_mtime, old_mtime))

    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("keep", "clean, recent")
