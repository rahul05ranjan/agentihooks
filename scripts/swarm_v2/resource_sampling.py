"""Measure one workload's whole process tree from /proc: CPU, RSS and PSS, process count and workdir storage."""

import os
import signal
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PROC = Path("/proc")
MIB = 1024 * 1024
PAGE = os.sysconf("SC_PAGE_SIZE")
TICKS = os.sysconf("SC_CLK_TCK")
CORE_WINDOW = 1.0
GONE = (FileNotFoundError, ProcessLookupError, PermissionError)
QUIET = {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
UNREAPED = os.WEXITED | os.WNOHANG | os.WNOWAIT


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    session: int
    ticks: int
    rss_bytes: int


@dataclass(frozen=True)
class Snapshot:
    at: float
    processes: int
    rss_bytes: int
    pss_bytes: int
    root_rss_bytes: int
    ticks: dict[int, int]


def read(proc: Path, pid: int) -> Process | None:
    entry = proc / str(pid)
    try:
        fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        pages = int((entry / "statm").read_text().split()[1])
    except GONE:
        return None
    return Process(pid, int(fields[1]), int(fields[3]), int(fields[11]) + int(fields[12]), pages * PAGE)


def pss(proc: Path, process: Process) -> int:
    try:
        text = (proc / str(process.pid) / "smaps_rollup").read_text()
    except GONE:
        return process.rss_bytes
    for line in text.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1]) * 1024
    return process.rss_bytes


def table(proc: Path) -> dict[int, Process]:
    found = (read(proc, int(entry.name)) for entry in proc.iterdir() if entry.name.isdigit())
    return {process.pid: process for process in found if process}


def members(processes: dict[int, Process], root: int) -> list[Process]:
    if root not in processes:
        return []
    children: dict[int, list[int]] = {}
    for process in processes.values():
        children.setdefault(process.ppid, []).append(process.pid)
    found = [root]
    for pid in found:
        found.extend(child for child in children.get(pid, ()) if child not in found)
    session = {p.pid for p in processes.values() if p.session == root}
    return [processes[pid] for pid in sorted(session.union(found))]


def snapshot(proc: Path, root: int, at: float) -> Snapshot:
    processes = table(proc)
    tree = members(processes, root)
    return Snapshot(
        at,
        len(tree),
        sum(p.rss_bytes for p in tree),
        sum(pss(proc, p) for p in tree),
        processes[root].rss_bytes if tree else 0,
        {p.pid: p.ticks for p in tree},
    )


@dataclass
class Peaks:
    processes: int = 0
    rss_bytes: int = 0
    pss_bytes: int = 0
    parent_rss_bytes: int = 0
    children_rss_bytes: int = 0
    storage_bytes: int = 0
    cores: float = 0.0
    samples: int = 0
    ticks: dict[int, int] = field(default_factory=dict)
    anchor: tuple[float, int] | None = None

    def add(self, snap: Snapshot, storage_bytes: int) -> None:
        self.samples += 1
        self.processes = max(self.processes, snap.processes)
        self.rss_bytes = max(self.rss_bytes, snap.rss_bytes)
        self.pss_bytes = max(self.pss_bytes, snap.pss_bytes)
        self.parent_rss_bytes = max(self.parent_rss_bytes, snap.root_rss_bytes)
        self.children_rss_bytes = max(self.children_rss_bytes, snap.rss_bytes - snap.root_rss_bytes)
        self.store(storage_bytes)
        for pid, ticks in snap.ticks.items():
            self.ticks[pid] = max(ticks, self.ticks.get(pid, 0))
        total = sum(self.ticks.values())
        if self.anchor is None:
            self.anchor = (snap.at, total)
        elif snap.at - self.anchor[0] >= CORE_WINDOW:
            self.cores = max(self.cores, (total - self.anchor[1]) / TICKS / (snap.at - self.anchor[0]))
            self.anchor = (snap.at, total)

    def store(self, storage_bytes: int) -> None:
        self.storage_bytes = max(self.storage_bytes, storage_bytes)


def _size(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def directory_bytes(path: Path) -> int:
    return sum(_size(entry) for entry in path.rglob("*"))


def _stop(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _exited(pid: int) -> bool:
    return os.waitid(os.P_PID, pid, UNREAPED) is not None


def summary(peaks: Peaks, usage, wall_seconds: float, status: int, timed_out: bool) -> dict:
    cpu_seconds = max(usage.ru_utime + usage.ru_stime, sum(peaks.ticks.values()) / TICKS)
    return {
        "peak_rss_mib": round(peaks.rss_bytes / MIB, 2),
        "peak_pss_mib": round(peaks.pss_bytes / MIB, 2),
        "peak_parent_rss_mib": round(peaks.parent_rss_bytes / MIB, 2),
        "peak_children_rss_mib": round(peaks.children_rss_bytes / MIB, 2),
        "peak_processes": peaks.processes,
        "peak_cores": round(max(peaks.cores, cpu_seconds / wall_seconds), 3),
        "cpu_seconds": round(cpu_seconds, 3),
        "wall_seconds": round(wall_seconds, 3),
        "peak_storage_mib": round(peaks.storage_bytes / MIB, 2),
        "samples": peaks.samples,
        "exit_code": os.waitstatus_to_exitcode(status),
        "timed_out": timed_out,
    }


def measure(
    command: Sequence[str], workdir: Path, env: Mapping[str, str], interval: float, timeout: float, proc: Path = PROC
) -> dict:
    """Run command in its own session under workdir, sampling its tree until it exits or the timeout kills it.

    The leader is checked without being reaped, so its process group still exists when stragglers are killed.
    """
    started = time.monotonic()
    child = subprocess.Popen(list(command), cwd=workdir, env=dict(env), **QUIET)
    peaks = Peaks()
    while not _exited(child.pid) and time.monotonic() - started <= timeout:
        peaks.add(snapshot(proc, child.pid, time.monotonic()), directory_bytes(workdir))
        time.sleep(interval)
    timed_out = not _exited(child.pid)
    _stop(child.pid)
    _, status, usage = os.wait4(child.pid, 0)
    child.returncode = os.waitstatus_to_exitcode(status)
    peaks.store(directory_bytes(workdir))
    return summary(peaks, usage, time.monotonic() - started, status, timed_out)
