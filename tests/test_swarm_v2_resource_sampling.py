import os
import sys
from pathlib import Path

import pytest

from scripts.swarm_v2 import resource_sampling as sampling

pytestmark = pytest.mark.unit

WORKLOADS = Path(__file__).parent / "performance" / "swarm_v2" / "workloads"
COMM = "w (x) y"


def proc_entry(proc: Path, pid: int, ppid: int, session: int, ticks=(0, 0), pages=0, pss_kb=None):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    rest = ["S", ppid, ppid, session, 0, -1, 4194304, 0, 0, 0, 0, ticks[0], ticks[1], 0, 0, 20, 0, 1, 0, 100]
    (entry / "stat").write_text(f"{pid} ({COMM}) " + " ".join(str(v) for v in rest) + "\n")
    (entry / "statm").write_text(f"1000 {pages} 10 1 0 50 0\n")
    if pss_kb is not None:
        (entry / "smaps_rollup").write_text(
            f"00400000-ffff [rollup]\nRss: 999 kB\nPss: {pss_kb} kB\nShared_Clean: 1 kB\n"
        )


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "self").mkdir()
    (root / "meminfo").write_text("MemTotal: 1 kB\n")
    return root


def test_read_parses_a_process_whose_name_holds_parentheses(proc):
    proc_entry(proc, 40, 7, 40, ticks=(30, 12), pages=5, pss_kb=8)
    assert sampling.read(proc, 40) == sampling.Process(40, 7, 40, 42, 5 * sampling.PAGE, 8 * 1024)


def test_read_falls_back_to_rss_when_pss_is_unreadable(proc):
    proc_entry(proc, 41, 7, 40, pages=3)
    assert sampling.read(proc, 41).pss_bytes == 3 * sampling.PAGE


def test_read_treats_a_vanished_process_as_gone(proc):
    (proc / "42").mkdir()
    assert sampling.read(proc, 42) is None
    assert sampling.read(proc, 43) is None


def test_read_treats_a_reaped_process_as_gone(proc, monkeypatch):
    proc_entry(proc, 44, 7, 40)
    real = Path.read_text

    def reaped(path, *args, **kwargs):
        if path.name == "statm":
            raise ProcessLookupError(3, "No such process")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reaped)
    assert sampling.read(proc, 44) is None


def test_table_lists_only_numeric_process_entries(proc):
    proc_entry(proc, 50, 1, 50)
    proc_entry(proc, 51, 50, 50)
    assert sorted(sampling.table(proc)) == [50, 51]


def test_members_follow_children_and_the_root_session_only(proc):
    proc_entry(proc, 10, 1, 10)
    proc_entry(proc, 11, 10, 10)
    proc_entry(proc, 12, 11, 12)
    proc_entry(proc, 13, 1, 10)
    proc_entry(proc, 14, 1, 14)
    proc_entry(proc, 15, 14, 14)
    members = sampling.members(sampling.table(proc), 10)
    assert sorted(p.pid for p in members) == [10, 11, 12, 13]


def test_members_of_a_gone_root_are_empty(proc):
    proc_entry(proc, 14, 1, 14)
    assert sampling.members(sampling.table(proc), 10) == []


def test_snapshot_sums_the_tree(proc):
    proc_entry(proc, 10, 1, 10, ticks=(5, 5), pages=2, pss_kb=4)
    proc_entry(proc, 11, 10, 10, ticks=(1, 2), pages=3)
    snap = sampling.snapshot(proc, 10, at=2.5)
    assert snap == sampling.Snapshot(2.5, 2, 5 * sampling.PAGE, 4 * 1024 + 3 * sampling.PAGE, {10: 10, 11: 3})


def test_peaks_keep_maxima_and_count_cores_from_tick_deltas():
    peaks = sampling.Peaks()
    peaks.add(sampling.Snapshot(0.0, 2, 100, 80, {1: 10, 2: 10}))
    peaks.add(sampling.Snapshot(0.5, 3, 300, 90, {1: 10 + sampling.TICKS, 2: 10, 3: sampling.TICKS // 2}))
    peaks.add(sampling.Snapshot(1.5, 1, 50, 40, {1: 10 + 2 * sampling.TICKS}))
    assert (peaks.processes, peaks.rss_bytes, peaks.pss_bytes) == (3, 300, 90)
    assert peaks.cores == pytest.approx(3.0)
    assert peaks.ticks == {1: 10 + 2 * sampling.TICKS, 2: 10, 3: sampling.TICKS // 2}


def test_peaks_ignore_a_snapshot_at_the_same_instant():
    peaks = sampling.Peaks()
    peaks.add(sampling.Snapshot(1.0, 1, 1, 1, {1: 0}))
    peaks.add(sampling.Snapshot(1.0, 1, 1, 1, {1: 500}))
    assert peaks.cores == 0.0


def test_directory_bytes_counts_regular_files_and_not_link_targets(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one").write_bytes(b"x" * 1000)
    (tmp_path / "two").write_bytes(b"y" * 24)
    (tmp_path / "link").symlink_to(tmp_path / "a" / "one")
    assert sampling.directory_bytes(tmp_path) == 1024
    assert sampling.directory_bytes(tmp_path / "missing") == 0


def test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path):
    command = [sys.executable, str(WORKLOADS / "memory.py"), "--children", "2", "--mib", "48", "--hold", "1.0"]
    result = sampling.measure(command, tmp_path, {"PATH": os.environ["PATH"]}, interval=0.05, timeout=60)
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert result["peak_processes"] >= 3
    assert result["peak_rss_mib"] >= 96
    assert result["peak_pss_mib"] >= 96
    assert result["cpu_seconds"] > 0
    assert result["samples"] >= 5
    assert result["wall_seconds"] >= 1.0


def test_measure_counts_peak_storage_in_the_workdir(tmp_path):
    script = "import time, pathlib; pathlib.Path('blob').write_bytes(bytes(3 << 20)); time.sleep(0.5)"
    result = sampling.measure([sys.executable, "-c", script], tmp_path, {}, interval=0.05, timeout=30)
    assert result["peak_storage_mib"] == pytest.approx(3.0, abs=0.01)


def test_measure_kills_the_group_on_timeout(tmp_path):
    marker = tmp_path / "pid"
    script = f"import os, subprocess, time; p = subprocess.Popen(['sleep', '60']); open({str(marker)!r}, 'w').write(str(p.pid)); time.sleep(60)"
    result = sampling.measure(
        [sys.executable, "-c", script], tmp_path, {"PATH": os.environ["PATH"]}, interval=0.05, timeout=1.0
    )
    assert result["timed_out"] and result["exit_code"] != 0
    child = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
