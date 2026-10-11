"""Measure one workload's whole process tree from /proc: CPU, RSS and PSS, process count and workdir storage."""

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PROC = Path("/proc")
MIB = 1024 * 1024
PAGE = os.sysconf("SC_PAGE_SIZE")
TICKS = os.sysconf("SC_CLK_TCK")
STORAGE_EVERY = 5
CORE_WINDOW = 1.0
GONE = (FileNotFoundError, ProcessLookupError, PermissionError)


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    session: int
    ticks: int
    rss_bytes: int
    pss_bytes: int


@dataclass(frozen=True)
class Snapshot:
    at: float
    processes: int
    rss_bytes: int
    pss_bytes: int
    ticks: dict[int, int]


def _pss(entry: Path) -> int | None:
    try:
        text = (entry / "smaps_rollup").read_text()
    except GONE:
        return None
    for line in text.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1]) * 1024
    return None


def read(proc: Path, pid: int) -> Process | None:
    entry = proc / str(pid)
    try:
        fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        pages = int((entry / "statm").read_text().split()[1])
    except GONE:
        return None
    rss = pages * PAGE
    pss = _pss(entry)
    return Process(
        pid, int(fields[1]), int(fields[3]), int(fields[11]) + int(fields[12]), rss, rss if pss is None else pss
    )


def table(proc: Path) -> dict[int, Process]:
    found = (read(proc, int(entry.name)) for entry in proc.iterdir() if entry.name.isdigit())
    return {process.pid: process for process in found if process}


def members(processes: dict[int, Process], root: int) -> list[Process]:
    if root not in processes:
        return []
    children: dict[int, list[int]] = {}
    for process in processes.values():
        children.setdefault(process.ppid, []).append(process.pid)
    found, pending = set(), [root]
    while pending:
        pid = pending.pop()
        found.add(pid)
        pending.extend(child for child in children.get(pid, ()) if child not in found)
    found |= {p.pid for p in processes.values() if p.session == root}
    return [processes[pid] for pid in sorted(found)]


def snapshot(proc: Path, root: int, at: float) -> Snapshot:
    tree = members(table(proc), root)
    return Snapshot(
        at,
        len(tree),
        sum(p.rss_bytes for p in tree),
        sum(p.pss_bytes for p in tree),
        {p.pid: p.ticks for p in tree},
    )


@dataclass
class Peaks:
    processes: int = 0
    rss_bytes: int = 0
    pss_bytes: int = 0
    cores: float = 0.0
    ticks: dict[int, int] = field(default_factory=dict)
    anchor: Snapshot | None = None

    def add(self, snap: Snapshot) -> None:
        self.processes = max(self.processes, snap.processes)
        self.rss_bytes = max(self.rss_bytes, snap.rss_bytes)
        self.pss_bytes = max(self.pss_bytes, snap.pss_bytes)
        for pid, ticks in snap.ticks.items():
            self.ticks[pid] = max(ticks, self.ticks.get(pid, 0))
        if self.anchor is None:
            self.anchor = snap
        elif snap.at - self.anchor.at >= CORE_WINDOW:
            used = sum(ticks - self.anchor.ticks.get(pid, 0) for pid, ticks in snap.ticks.items())
            self.cores = max(self.cores, used / TICKS / (snap.at - self.anchor.at))
            self.anchor = snap


def directory_bytes(path: Path) -> int:
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                info = os.lstat(os.path.join(folder, name))
            except FileNotFoundError:
                continue
            if not os.path.islink(os.path.join(folder, name)):
                total += info.st_size
    return total


def _stop(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def measure(
    command: Sequence[str],
    workdir: Path,
    env: Mapping[str, str],
    interval: float = 0.1,
    timeout: float = 600.0,
    proc: Path = PROC,
) -> dict:
    """Run command in its own session under workdir, sampling its tree until it exits or the timeout kills it."""
    started = time.monotonic()
    child = subprocess.Popen(
        list(command),
        cwd=workdir,
        env=dict(env),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    peaks, storage, samples, timed_out = Peaks(), 0, 0, False
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        now = time.monotonic()
        if pid:
            break
        if now - started > timeout:
            timed_out = True
            _stop(child.pid)
            pid, status, usage = os.wait4(child.pid, 0)
            break
        peaks.add(snapshot(proc, child.pid, now))
        if samples % STORAGE_EVERY == 0:
            storage = max(storage, directory_bytes(workdir))
        samples += 1
        time.sleep(interval)
    child.returncode = os.waitstatus_to_exitcode(status)
    _stop(child.pid)
    storage = max(storage, directory_bytes(workdir))
    tracked = sum(peaks.ticks.values()) / TICKS
    return {
        "peak_rss_mib": round(peaks.rss_bytes / MIB, 2),
        "peak_pss_mib": round(peaks.pss_bytes / MIB, 2),
        "peak_processes": peaks.processes,
        "peak_cores": round(peaks.cores, 3),
        "cpu_seconds": round(max(usage.ru_utime + usage.ru_stime, tracked), 3),
        "wall_seconds": round(time.monotonic() - started, 3),
        "peak_storage_mib": round(storage / MIB, 2),
        "samples": samples,
        "exit_code": child.returncode,
        "timed_out": timed_out,
    }
