import json
import subprocess
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")

AT = 1_800_000_000_000
HOUR = 3_600_000
REPO = "owner/repo"


def iso(at):
    return datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()


@pytest.fixture
def rig(monkeypatch):
    import fakeredis

    from scripts.swarm import status
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("proof", "/repo", max_eng=0, max_ci=0)
    store.create(config)
    state = {"at": AT, "queue": 3, "merged": AT - HOUR, "calls": []}
    monkeypatch.setattr(status, "now_ms", lambda: state["at"])
    monkeypatch.setattr(status.activity, "counts", lambda slug: {})

    def run(command, **kwargs):
        state["calls"].append((command, kwargs))
        if command[1:3] == ["repo", "view"]:
            data = {"nameWithOwner": REPO}
        elif command[2] == "graphql":
            data = {
                "data": {
                    "repository": {
                        "mergeQueue": {"entries": {"totalCount": state["queue"]}},
                        "pullRequests": {
                            "nodes": [{"mergedAt": iso(state["merged"]), "updatedAt": iso(state["merged"])}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        elif "/actions/runs?" in command[2]:
            data = {"workflow_runs": [{"id": 11, "conclusion": "failure"}, {"id": 12, "conclusion": "failure"}]}
        elif "/actions/runs/11/jobs?" in command[2]:
            data = {"jobs": [{"name": "Coverage", "conclusion": "failure"}, {"name": "Tests", "conclusion": "success"}]}
        elif "/actions/runs/12/jobs?" in command[2]:
            data = {
                "jobs": [{"name": "Coverage", "conclusion": "failure"}, {"name": "Mutation", "conclusion": "failure"}]
            }
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", run)
    return store, config, state


def test_health_pass_reports_stalled_merge_queue_and_notifies_master(rig):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status

    store, config, state = rig
    found = status.findings(store, "proof", config, [], [])
    assert [(f["kind"], f["subject"], f["evidence"]) for f in found] == [
        (
            "merge stall",
            REPO,
            [
                "Queue length: 3",
                "Time since last dev merge: 60 minutes",
                "Most common failed check in the last 10 queue runs: Coverage (2 runs)",
            ],
        )
    ]
    inbox = InboxStore(store.redis)
    sent = ledger_events.findings_pass(inbox, store, "proof", found)
    assert len(sent) == 1
    assert "Queue length: 3" in sent[0]
