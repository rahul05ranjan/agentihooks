import copy
import cProfile
import io
import json
import pstats
import types

import pytest

from tests.swarm_ledger import operations_cpu_replay as replay_module

pytestmark = pytest.mark.xdist_group("fakeredis")
TASKS = 2543
TASK_BYTES = 13885685
FOLLOWUPS = 2431


def scaled(recorded):
    state = copy.deepcopy(recorded["state"])
    tasks = state["tasks"]
    base = len(tasks)
    for index in range(TASKS - base):
        row = copy.deepcopy(tasks[index % base])
        row["id"] = f"tz{index}"
        tasks.append(row)
    pad = max(0, (TASK_BYTES - len(json.dumps(tasks))) // len(tasks))
    for row in tasks:
        row["description"] = (row.get("description") or "") + "x" * pad
    followups = state["followups"]
    base = len(followups)
    for index in range(FOLLOWUPS - base):
        row = copy.deepcopy(followups[index % base])
        row["id"] = f"fz{index}"
        followups.append(row)
    return {**recorded, "state": state}


def profiled_run(tmp_path, monkeypatch, recorded):
    profiles = []

    class Capturing(cProfile.Profile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            profiles.append(self)

    monkeypatch.setattr(replay_module, "corpus", lambda: recorded)
    monkeypatch.setattr(replay_module, "cProfile", types.SimpleNamespace(Profile=Capturing))
    report = replay_module.replay(tmp_path, monkeypatch, profiled=True)
    stream = io.StringIO()
    stats = pstats.Stats(profiles[0], stream=stream)
    stats.sort_stats("tottime").print_stats(30)
    stats.sort_stats("cumulative").print_stats(45)
    return {
        "bytes": len(json.dumps(recorded["state"])),
        "tasks": len(recorded["state"]["tasks"]),
        "requests": report["requests"],
        "mean_cpu_seconds": report["mean_cpu_seconds"],
        "median_cpu_seconds": sorted(report["cpu_seconds"])[len(report["cpu_seconds"]) // 2],
        "phases": report["phase_cpu_seconds_per_request"],
        "per_op": [
            [op["op"], round(seconds, 4)] for op, seconds in zip(recorded["ops"], report["cpu_seconds"], strict=True)
        ],
        "profile": stream.getvalue(),
    }


def test_profile_operations_writes_on_recorded_and_scaled_ledgers(tmp_path, monkeypatch):
    recorded = replay_module.corpus()
    results = {}
    for name, corpus in (("recorded", recorded), ("scaled", scaled(recorded))):
        with monkeypatch.context() as isolated:
            results[name] = profiled_run(tmp_path / name, isolated, corpus)
    lines = []
    for name, result in results.items():
        lines.append(f"===== {name} =====")
        lines.append(json.dumps({key: value for key, value in result.items() if key != "profile"}))
        lines.append(result["profile"])
    pytest.fail("\n".join(lines), pytrace=False)
