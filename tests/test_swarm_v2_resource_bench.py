import json
import sys
from pathlib import Path

import pytest

from scripts.swarm_v2 import resource_bench as bench

pytestmark = pytest.mark.unit

WORKLOADS = Path(__file__).parent / "performance" / "swarm_v2" / "workloads"


@pytest.mark.parametrize(
    ("text", "mib"),
    [
        ("16078424Ki", 15701),
        ("1Gi", 1024),
        ("128Mi", 128),
        ("1G", 953),
        ("97743690881", 93215),
        ("2e9", 1907),
        ("0", 0),
    ],
)
def test_memory_quantities_become_whole_mib(text, mib):
    assert bench.mib(text) == mib


@pytest.mark.parametrize(
    ("text", "millis"), [("4", 4000), ("500m", 500), ("0.25", 250), ("1500000u", 1500), ("100n", 1)]
)
def test_cpu_quantities_become_millis(text, millis):
    assert bench.millis(text) == millis


def pod(node, phase="Running", containers=(("100m", "64Mi"),), init=()):
    def spec(cpu, memory):
        return {"resources": {"requests": {"cpu": cpu, "memory": memory}}}

    return {
        "spec": {
            "nodeName": node,
            "containers": [spec(*c) for c in containers],
            "initContainers": [spec(*c) for c in init],
        },
        "status": {"phase": phase},
    }


def test_nodes_subtract_scheduled_pod_requests_per_node():
    listing = {
        "items": [
            {
                "metadata": {
                    "name": "n1",
                    "labels": {"anton.io/pool": "general", "node.kubernetes.io/instance-type": "m6i.xlarge"},
                },
                "status": {"allocatable": {"cpu": "4", "memory": "16Gi", "ephemeral-storage": "100Gi"}},
            },
            {"metadata": {"name": "n2"}, "status": {"allocatable": {"cpu": "2", "memory": "8Gi"}}},
        ]
    }
    pods = {
        "items": [
            pod("n1", containers=(("250m", "512Mi"), ("250m", "512Mi"))),
            pod("n1", containers=(("100m", "64Mi"),), init=(("2", "2Gi"),)),
            pod("n1", phase="Succeeded", containers=(("3", "8Gi"),)),
            pod("", containers=(("3", "8Gi"),)),
            {"spec": {"nodeName": "n2", "containers": [{"name": "bare"}]}, "status": {"phase": "Pending"}},
        ]
    }
    assert bench.nodes(listing, pods) == [
        {
            "name": "n1",
            "pool": "general",
            "instance_type": "m6i.xlarge",
            "allocatable_memory_mib": 16384,
            "allocatable_cpu_millis": 4000,
            "allocatable_ephemeral_mib": 102400,
            "colocated_memory_mib": 3072,
            "colocated_cpu_millis": 2500,
            "colocated_pods": 2,
        },
        {
            "name": "n2",
            "pool": "",
            "instance_type": "",
            "allocatable_memory_mib": 8192,
            "allocatable_cpu_millis": 2000,
            "allocatable_ephemeral_mib": 0,
            "colocated_memory_mib": 0,
            "colocated_cpu_millis": 0,
            "colocated_pods": 1,
        },
    ]


def suite(tmp_path, workloads, repeats=1):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"suite": "fixture", "repeats": repeats, "interval": 0.05, "workloads": workloads}))
    return path


def test_run_measures_each_child_count_and_repeat_in_a_fresh_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("SV2_FAKE_TOKEN", "fake-token-value")
    probe = "import os, sys; sys.exit(7 if any('FAKE' in k for k in os.environ) else 0)"
    path = suite(
        tmp_path,
        [
            {
                "name": "memory",
                "class": "memory",
                "version": "1",
                "children": [1, 2],
                "command": [
                    "{python}",
                    str(WORKLOADS / "memory.py"),
                    "--children",
                    "{children}",
                    "--mib",
                    "16",
                    "--hold",
                    "0.3",
                ],
            },
            {"name": "probe", "class": "idle", "version": "1", "children": [0], "command": ["{python}", "-c", probe]},
        ],
        repeats=2,
    )
    root = tmp_path / "work"
    result = bench.run(path, root, generation=7, tools={"python": [sys.executable, "--version"]})
    assert result["suite"] == "fixture" and result["generation"] == 7
    assert result["tools"] == {
        "python": f"Python {sys.version.split()[0]}",
        "workload:memory": "1",
        "workload:probe": "1",
    }
    assert [(r["workload"], r["children"], r["repeat"]) for r in result["runs"]] == [
        ("memory", 1, 1),
        ("memory", 1, 2),
        ("memory", 2, 1),
        ("memory", 2, 2),
        ("probe", 0, 1),
        ("probe", 0, 2),
    ]
    assert all(r["exit_code"] == 0 for r in result["runs"])
    assert result["runs"][2]["peak_processes"] >= 3
    assert result["host"]["cpus"] >= 1 and result["host"]["memory_total_mib"] > 0
    assert "fake-token-value" not in json.dumps(result)
    assert list(root.iterdir()) == []


def test_tool_versions_record_a_missing_tool_as_absent():
    assert bench.versions({"none": ["/nonexistent/tool", "--version"]}) == {"none": "absent"}


def test_cli_runs_derives_and_renders(tmp_path):
    path = suite(
        tmp_path,
        [
            {
                "name": "memory",
                "class": "memory",
                "version": "1",
                "children": [1],
                "command": [
                    "{python}",
                    str(WORKLOADS / "memory.py"),
                    "--children",
                    "{children}",
                    "--mib",
                    "8",
                    "--hold",
                    "0.2",
                ],
            }
        ],
        repeats=3,
    )
    measurements = tmp_path / "measurements.json"
    assert (
        bench.main(
            [
                "run",
                "--suite",
                str(path),
                "--root",
                str(tmp_path / "w"),
                "--generation",
                "3",
                "--output",
                str(measurements),
            ]
        )
        == 0
    )
    listing = tmp_path / "nodes-listing.json"
    listing.write_text(
        json.dumps({"items": [{"metadata": {"name": "n"}, "status": {"allocatable": {"cpu": "4", "memory": "16Gi"}}}]})
    )
    pods = tmp_path / "pods.json"
    pods.write_text(json.dumps({"items": []}))
    nodes = tmp_path / "nodes.json"
    assert bench.main(["nodes", "--nodes", str(listing), "--pods", str(pods), "--output", str(nodes)]) == 0
    report, page = tmp_path / "report.json", tmp_path / "report.md"
    args = [
        "derive",
        "--measurements",
        str(measurements),
        "--nodes",
        str(nodes),
        "--output",
        str(report),
        "--markdown",
        str(page),
    ]
    assert bench.main([*args, "--min-repeats", "3"]) == 0
    derived = json.loads(report.read_text())
    assert sorted(derived["profiles"]) == ["memory"] and derived["generation"] == 3
    assert "| memory |" in page.read_text()


def test_cli_derive_refuses_an_idle_only_measurement(tmp_path, capsys):
    measurements = tmp_path / "m.json"
    idle = {"workload": "idle", "class": "idle", "children": 0, "repeat": 1, "peak_rss_mib": 40.0, "peak_processes": 1}
    measurements.write_text(json.dumps({"generation": 1, "tools": {}, "runs": [idle]}))
    nodes = tmp_path / "n.json"
    nodes.write_text('{"nodes": []}')
    report = tmp_path / "r.json"
    code = bench.main(["derive", "--measurements", str(measurements), "--nodes", str(nodes), "--output", str(report)])
    assert code == 2 and not report.exists()
    assert "an idle sample cannot size a worker" in capsys.readouterr().err


def test_cli_promote_prints_the_decision_and_keeps_the_accepted_file(tmp_path, capsys):
    accepted = tmp_path / "accepted.json"
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"generation": 1, "profiles": {}, "tools": {}}))
    assert bench.main(["promote", "--accepted", str(accepted), "--candidate", str(candidate)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "promoted"
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"generation": 0, "profiles": {}, "tools": {}}))
    assert bench.main(["promote", "--accepted", str(accepted), "--candidate", str(stale)]) == 1
    assert json.loads(capsys.readouterr().out)["decision"] == "stale"
