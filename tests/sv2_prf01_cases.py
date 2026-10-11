import copy
import hashlib
import json
import tempfile
from pathlib import Path

from scripts.swarm_v2 import resource_profiles as profiles

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "worker-resources"
INPUTS = ("measurements.json", "update.json", "nodes.json", "disclosure.json")
POLICY = profiles.Policy()
EVIDENCE_CLASS = (
    "measured: the workloads ran on a GitHub hosted Ubuntu runner in the Worker resources workflow named in "
    "measurements.json and update.json, and node capacity was read from the Anton cluster with kubectl as named in "
    "nodes.json; derivation, coexistence and promotion are pure functions over those committed records; no worker Pod "
    "ran the workloads and no profile was applied to a deployed Pod policy"
)
NOTES = (
    "Measured on a GitHub hosted runner, not inside a worker Pod: CPU peaks are bounded by the runner's cores.",
    "The agent CLI's own resident memory is a declared reserve, not a measurement; it needs a live agent run.",
    "Coexistence counts whole workers in each node's allocatable capacity minus the requests already scheduled there.",
)


def load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


def derive(measured: dict) -> dict:
    return profiles.derive(copy.deepcopy(measured), load("nodes.json")["nodes"], POLICY)


def results_page() -> str:
    return profiles.markdown(derive(load("measurements.json")), NOTES)


def _summary(report: dict) -> dict:
    return {
        "generation": report["generation"],
        "profiles": {name: built["limits"] for name, built in report["profiles"].items()},
        "samples": {name: built["uncertainty"]["samples"] for name, built in report["profiles"].items()},
        "refused": report["refused"],
        "fits": {row["node"]: row["fits"] for row in report["coexistence"]},
        "worker_process_tree_peak_memory": report["worker_process_tree_peak_memory"],
    }


def _accept(report: dict, *candidates: dict) -> tuple[list[str], bool]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "accepted.json"
        decisions = [profiles.accept(path, report, POLICY)["decision"]]
        before = path.read_bytes()
        decisions += [profiles.accept(path, candidate, POLICY)["decision"] for candidate in candidates]
        return decisions, path.read_bytes() == before


def _positive() -> tuple[dict, bool]:
    measured = load("measurements.json")
    first, second = derive(measured), derive(measured)
    decisions, _ = _accept(first)
    admitted = set(first["profiles"])
    floor = {
        name: POLICY.min_repeats * len({r["children"] for r in measured["runs"] if r["class"] == name})
        for name in admitted
    }
    observed = {**_summary(first), "independent_rerun_matches": first == second, "accepted": decisions}
    checks = [
        admitted == set(profiles.CLASSES),
        all(first["profiles"][n]["measured"]["peak_rss_mib"]["n"] >= floor[n] for n in admitted),
        all(first["profiles"][n]["uncertainty"]["samples"] >= floor[n] for n in admitted),
        first["refused"] == {"idle": profiles.IDLE_REFUSAL},
        isinstance(first["coexistence"], list) and len(first["coexistence"]) == len(load("nodes.json")["nodes"]),
        all(set(row["fits"]) == admitted for row in first["coexistence"]),
        first == second,
        decisions == ["promoted"],
    ]
    return observed, all(checks)


def _rejection() -> tuple[dict, bool]:
    measured = load("measurements.json")
    idle = {**measured, "runs": [r for r in measured["runs"] if r["class"] == profiles.IDLE]}
    accepted = derive(measured)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "accepted.json"
        profiles.accept(path, accepted, POLICY)
        before = path.read_bytes()
        try:
            derive(idle)
            refusal = None
        except profiles.ClaimRefused as refused:
            refusal = {"message": str(refused), "refused": refused.refused}
        unchanged = path.read_bytes() == before
    secret = "-".join(load("disclosure.json")["fake_secret_parts"])
    texts = [(FIXTURES / name).read_text() for name in ("measurements.json", "update.json")]
    leaked = [name for name, text in zip(("measurements", "update"), texts) if secret in text]
    observed = {
        "idle_runs": len(idle["runs"]),
        "idle_peak_rss_mib": max(r["peak_rss_mib"] for r in idle["runs"]),
        "refusal": refusal,
        "accepted_unchanged": unchanged,
        "fake_secret_in_records": leaked,
        "fake_secret_set_in_run": load("disclosure.json")["variable"],
    }
    checks = [
        len(idle["runs"]) >= POLICY.min_repeats,
        refusal is not None and refusal["refused"] == {"idle": profiles.IDLE_REFUSAL},
        unchanged,
        leaked == [],
    ]
    return observed, all(checks)


def _updated(measured: dict, update: dict) -> dict:
    classes = {r["class"] for r in update["runs"]}
    runs = [r for r in measured["runs"] if r["class"] not in classes] + update["runs"]
    return {
        **measured,
        "generation": update["generation"],
        "tools": {**measured["tools"], **update["tools"]},
        "runs": runs,
    }


def _recovery() -> tuple[dict, bool]:
    measured, update = load("measurements.json"), load("update.json")
    accepted = derive(measured)
    candidate = derive(_updated(measured, update))
    stale = derive({**measured, "generation": measured["generation"] - 1})
    partial = derive(
        {
            **measured,
            "generation": update["generation"] + 1,
            "runs": [r for r in measured["runs"] if r["class"] != "browser"],
        }
    )
    decisions, unchanged = _accept(accepted, candidate, stale, partial, json.loads(json.dumps(accepted)))
    changes = profiles.compare(accepted, candidate, POLICY)
    observed = {
        "accepted_generation": accepted["generation"],
        "candidate_generation": candidate["generation"],
        "decisions": dict(zip(("first", "toolchain_update", "stale", "missing_class", "replay"), decisions)),
        "changes": changes,
        "missing": profiles.compare(accepted, partial, POLICY)["missing"],
        "accepted_unchanged": unchanged,
        "candidate_memory_limit": candidate["profiles"]["memory"]["limits"],
        "accepted_memory_limit": accepted["profiles"]["memory"]["limits"],
    }
    checks = [
        decisions == ["promoted", "requalify", "stale", "requalify", "replayed"],
        any(row[0] == "memory" for row in changes["material"]),
        set(changes["tools_changed"]) >= {"workload:memory"},
        observed["missing"] == ["browser"],
        unchanged,
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-PRF-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
