"""Run a worker resource suite, read node capacity, derive profiles and gate their promotion."""

import argparse
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.swarm_v2 import resource_profiles as profiles
from scripts.swarm_v2 import resource_sampling as sampling

SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
}
DONE_PHASES = ("Succeeded", "Failed")
PASSED_ENV = ("PATH", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
TOOLS = {
    "python": [sys.executable, "--version"],
    "pytest": [sys.executable, "-m", "pytest", "--version"],
    "playwright": [sys.executable, "-m", "playwright", "--version"],
    "cc": ["cc", "--version"],
    "kernel": ["uname", "-r"],
}


def _amount(text: str) -> Decimal:
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            return Decimal(text[: -len(suffix)]) * SUFFIXES[suffix]
    return Decimal(text)


def mib(text: str) -> int:
    return int(_amount(text) / sampling.MIB)


def millis(text: str) -> int:
    return math.ceil(_amount(text) * 1000)


def _requests(container: Mapping) -> tuple[Decimal, int]:
    requested = container.get("resources", {}).get("requests", {})
    return _amount(requested.get("memory", "0")), millis(requested.get("cpu", "0"))


def _pod_requests(pod: Mapping) -> tuple[Decimal, int]:
    spec = pod["spec"]
    main = [_requests(c) for c in spec.get("containers", [])]
    init = [_requests(c) for c in spec.get("initContainers", [])]
    memory = max([sum((m for m, _ in main), Decimal(0)), *(m for m, _ in init)])
    cpu = max([sum(c for _, c in main), *(c for _, c in init)])
    return memory, cpu


def nodes(listing: Mapping, pods: Mapping) -> list[dict]:
    """Each node's allocatable capacity and the requests of the Pods already scheduled on it."""
    used: dict[str, list] = {}
    for pod in pods["items"]:
        node = pod["spec"].get("nodeName")
        if not node or pod.get("status", {}).get("phase") in DONE_PHASES:
            continue
        memory, cpu = _pod_requests(pod)
        total = used.setdefault(node, [Decimal(0), 0, 0])
        total[0] += memory
        total[1] += cpu
        total[2] += 1
    rows = []
    for item in listing["items"]:
        name, labels = item["metadata"]["name"], item["metadata"].get("labels", {})
        allocatable = item["status"]["allocatable"]
        memory, cpu, count = used.get(name, [Decimal(0), 0, 0])
        rows.append(
            {
                "name": name,
                "pool": labels.get("anton.io/pool", ""),
                "instance_type": labels.get("node.kubernetes.io/instance-type", ""),
                "allocatable_memory_mib": mib(allocatable["memory"]),
                "allocatable_cpu_millis": millis(allocatable["cpu"]),
                "allocatable_ephemeral_mib": mib(allocatable.get("ephemeral-storage", "0")),
                "colocated_memory_mib": math.ceil(memory / sampling.MIB),
                "colocated_cpu_millis": cpu,
                "colocated_pods": count,
            }
        )
    return rows


def versions(tools: Mapping[str, Sequence[str]]) -> dict[str, str]:
    found = {}
    for name, command in tools.items():
        try:
            done = subprocess.run(list(command), capture_output=True, text=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            found[name] = "absent"
            continue
        lines = [line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        found[name] = lines[0] if lines else "absent"
    return found


def _meminfo() -> dict[str, int]:
    rows = Path("/proc/meminfo").read_text().splitlines()
    return {line.split(":")[0]: int(line.split()[1]) // 1024 for line in rows}


def host() -> dict:
    memory = _meminfo()
    return {
        "cpus": os.cpu_count(),
        "memory_total_mib": memory["MemTotal"],
        "memory_available_mib": memory["MemAvailable"],
        "load_average_1m": round(os.getloadavg()[0], 2),
        "kernel": platform.release(),
    }


def _environment(workdir: Path, children: int) -> dict[str, str]:
    passed = {name: os.environ[name] for name in PASSED_ENV if name in os.environ}
    return {**passed, "HOME": str(workdir), "TMPDIR": str(workdir), "CHILDREN": str(children)}


def run(suite: Path, root: Path, generation: int, tools: Mapping[str, Sequence[str]] = TOOLS, source: str = "") -> dict:
    """Measure every workload at every child count and repeat, each in a fresh workdir removed afterwards."""
    plan = json.loads(suite.read_text())
    root.mkdir(parents=True, exist_ok=True)
    started = host()
    found = versions(tools)
    runs = []
    for workload in plan["workloads"]:
        found[f"workload:{workload['name']}"] = workload["version"]
        for children in workload["children"]:
            for repeat in range(1, plan["repeats"] + 1):
                workdir = Path(tempfile.mkdtemp(prefix=f"{workload['name']}-", dir=root))
                values = {"python": sys.executable, "children": children, "workloads": suite.parent, "workdir": workdir}
                command = [part.format(**values) for part in workload["command"]]
                env = _environment(workdir, children)
                measured = sampling.measure(command, workdir, env, plan["interval"], workload.get("timeout", 600))
                shutil.rmtree(workdir)
                runs.append(
                    {
                        "workload": workload["name"],
                        "class": workload["class"],
                        "children": children,
                        "repeat": repeat,
                        **measured,
                    }
                )
    return {
        "suite": plan["suite"],
        "generation": generation,
        "source": source,
        "host": {**started, "load_average_1m_after": round(os.getloadavg()[0], 2)},
        "tools": found,
        "runs": runs,
    }


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    measure = commands.add_parser("run")
    measure.add_argument("--suite", type=Path, required=True)
    measure.add_argument("--root", type=Path, required=True)
    measure.add_argument("--generation", type=int, required=True)
    measure.add_argument("--source", default="")
    measure.add_argument("--output", type=Path, required=True)
    capacity = commands.add_parser("nodes")
    capacity.add_argument("--nodes", type=Path, required=True)
    capacity.add_argument("--pods", type=Path, required=True)
    capacity.add_argument("--output", type=Path, required=True)
    build = commands.add_parser("derive")
    build.add_argument("--measurements", type=Path, required=True)
    build.add_argument("--nodes", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--markdown", type=Path)
    build.add_argument("--min-repeats", type=int, default=profiles.Policy.min_repeats)
    gate = commands.add_parser("promote")
    gate.add_argument("--accepted", type=Path, required=True)
    gate.add_argument("--candidate", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        _write(args.output, run(args.suite, args.root, args.generation, source=args.source))
        return 0
    if args.command == "nodes":
        rows = nodes(json.loads(args.nodes.read_text()), json.loads(args.pods.read_text()))
        _write(args.output, {"read_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "nodes": rows})
        return 0
    if args.command == "derive":
        policy = profiles.Policy(min_repeats=args.min_repeats)
        try:
            capacity = json.loads(args.nodes.read_text())["nodes"]
            report = profiles.derive(json.loads(args.measurements.read_text()), capacity, policy)
        except profiles.ClaimRefused as refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 2
        _write(args.output, report)
        if args.markdown:
            args.markdown.write_text(profiles.markdown(report))
        return 0
    outcome = profiles.accept(args.accepted, json.loads(args.candidate.read_text()), profiles.Policy())
    print(json.dumps({"decision": outcome["decision"], "changes": outcome["changes"]}))
    return 0 if outcome["decision"] in ("promoted", "replayed") else 1


if __name__ == "__main__":
    sys.exit(main())
