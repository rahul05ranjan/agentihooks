import cProfile
import hashlib
import importlib.metadata
import json
import platform
import pstats
import re
import resource
import sys
import time
from pathlib import Path

import pytest


class Results:
    def __init__(self):
        self.collected = []
        self.results = []

    def pytest_collection_modifyitems(self, items):
        self.collected = [item.nodeid for item in items]

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed:
            self.results.append((report.nodeid, report.when, report.outcome))


def cpu() -> float:
    usages = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    return sum(usage.ru_utime + usage.ru_stime for usage in usages)


def main() -> int:
    manifest, destination = (Path(arg).resolve() for arg in sys.argv[1:])
    nodes = sorted({re.sub(r"@[^\[\]]*$", "", node) for node in json.loads(manifest.read_text())})
    destination.mkdir(parents=True, exist_ok=True)
    results = Results()
    profiler = cProfile.Profile()
    arguments = [*nodes, "-n", "0", "-q", "--tb=short", "-p", "no:pytest_cov"]
    started, cpu_started = time.perf_counter(), cpu()
    profiler.enable()
    status = pytest.main(arguments, plugins=[results])
    profiler.disable()
    measured_cpu, elapsed = cpu() - cpu_started, time.perf_counter() - started
    profiler.dump_stats(str(destination / "profile.prof"))
    with (destination / "profile.txt").open("w") as stream:
        stats = pstats.Stats(profiler, stream=stream)
        stats.sort_stats("tottime").print_stats(50)
        stats.sort_stats("cumtime").print_stats("fakeredis|redis/", 50)
    report = {
        "cpu_seconds": measured_cpu,
        "wall_seconds": elapsed,
        "python": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("fakeredis", "redis", "pytest")},
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "test_files": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in sorted({node.split("::", 1)[0] for node in nodes})
        },
        "conftest_sha256": hashlib.sha256(Path("tests/conftest.py").read_bytes()).hexdigest(),
        "arguments": arguments,
        "collected": results.collected,
        "results": results.results,
        "exit_code": int(status),
    }
    (destination / "measurement.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key not in {"arguments", "collected", "results"}}))
    if set(results.collected) != set(nodes):
        return 1
    return int(status)


if __name__ == "__main__":
    sys.exit(main())
