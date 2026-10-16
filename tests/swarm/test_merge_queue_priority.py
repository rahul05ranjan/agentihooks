import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, merge_queue

pytestmark = pytest.mark.unit
URL = "https://github.com/o/r/pull/9"


class FakeQueue:
    def __init__(self, queued=False):
        self.entries = [self.entry(1, "AWAITING_CHECKS"), self.entry(2), self.entry(3)]
        if queued:
            self.entries += [self.entry(9), self.entry(4)]
        self.mutations = []

    def entry(self, number, state="QUEUED"):
        return {
            "id": f"E{number}",
            "state": state,
            "pullRequest": {"id": f"P{number}", "url": URL[:-1] + str(number), "headRefOid": f"head{number}"},
        }

    def run(self, command, **kwargs):
        if command[2] != "graphql":
            data = {"workflow_runs": [{"head_sha": "head9", "conclusion": "success"}]}
        else:
            args = dict(item.split("=", 1) for item in command[4::2])
            query = args.pop("query")
            if "enqueuePullRequest" in query:
                number = int(args["id"][1:])
                self.entries.append(self.entry(number))
                self.mutations.append(("queue", number))
                data = {"enqueuePullRequest": {"mergeQueueEntry": {"id": f"E{number}"}}}
            elif "dequeuePullRequest" in query:
                number = int(args["id"][1:])
                self.entries = [e for e in self.entries if e["pullRequest"]["id"] != args["id"]]
                self.mutations.append(("dequeue", number))
                data = {"dequeuePullRequest": {"mergeQueueEntry": {"id": f"E{number}"}}}
            elif "entries(" in query:
                data = {
                    "resource": {
                        "repository": {
                            "mergeQueue": {
                                "entries": {
                                    "nodes": self.entries.copy(),
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                }
            else:
                number = int(args["url"].rsplit("/", 1)[1])
                entry = next((e for e in self.entries if e["pullRequest"]["id"] == f"P{number}"), None)
                data = {
                    "resource": {
                        "id": f"P{number}",
                        "state": "OPEN",
                        "headRefOid": f"head{number}",
                        "baseRefName": "dev",
                        "mergeQueueEntry": entry,
                        "repository": {"nameWithOwner": "o/r", "mergeQueue": {"id": "Q"}},
                    }
                }
            data = {"data": data}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")


@pytest.mark.parametrize("queued, expected", [(False, [1, 9, 2, 3]), (True, [1, 9, 4, 2, 3])])
def test_priority_preserves_displaced_order_and_running_entries(queued, expected):
    queue = FakeQueue(queued)
    result = merge_queue.prioritize(URL, run=queue.run)
    assert [int(e["pullRequest"]["id"][1:]) for e in queue.entries] == expected
    assert queue.mutations == [("dequeue", 3), ("dequeue", 2)] + ([] if queued else [("queue", 9)]) + [
        ("queue", 2),
        ("queue", 3),
    ]
    assert result["displaced"] == [URL[:-1] + "2", URL[:-1] + "3"]


@pytest.mark.parametrize("errors, status", [([], 0), (["restore refused"], 1)])
def test_master_priority_notifies_displaced_owners(monkeypatch, capsys, errors, status):
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("master")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(
        cli,
        "LedgerClient",
        lambda: SimpleNamespace(tasks=lambda slug: [{"pr_url": URL[:-1] + "2", "claimed_by": "ci@owner"}]),
    )
    monkeypatch.setattr(
        merge_queue, "prioritize", lambda url: {"url": url, "displaced": [URL[:-1] + "2"], "errors": errors}
    )
    sent = []
    monkeypatch.setattr(
        cli,
        "InboxStore",
        lambda redis: SimpleNamespace(send=lambda sender, recipient, text: sent.append((sender, recipient, text))),
    )
    assert cli.main(["sw", "merge", "priority", URL]) == status
    assert len(sent) == 1
    assert sent[0][0:2] == ("engineer@a1b2c3-0001", "ci@owner")
    assert "original order" in sent[0][2]
    assert URL in sent[0][2]
    output = capsys.readouterr()
    if errors:
        assert "restore refused" in output.err
    else:
        assert json.loads(output.out)["displaced"] == [URL[:-1] + "2"]


def test_worker_cannot_prioritize(monkeypatch):
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("ci")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(merge_queue, "prioritize", lambda url: pytest.fail("worker reordered queue"))
    assert cli.main(["sw", "merge", "priority", URL]) == 1


@pytest.mark.parametrize("failure", ["critical", "dequeue", "timeout"])
def test_priority_restores_entries_after_a_mutation_fails(failure):
    queue = FakeQueue()

    def run(command, **kwargs):
        if ("enqueuePullRequest" in " ".join(command) and "id=P9" in command and failure == "critical") or (
            "dequeuePullRequest" in " ".join(command) and "id=P2" in command and failure == "dequeue"
        ):
            return subprocess.CompletedProcess(command, 1, "", "insertion refused")
        done = queue.run(command, **kwargs)
        if "dequeuePullRequest" in " ".join(command) and "id=P2" in command and failure == "timeout":
            raise subprocess.TimeoutExpired(command, 20)
        return done

    result = merge_queue.prioritize(URL, run=run)
    assert result["errors"]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P2", "P3"]


@pytest.mark.parametrize("base, queue_exists", [("main", True), ("dev", False)])
def test_priority_refuses_release_targets_and_missing_queues(base, queue_exists):
    from scripts.swarm.store import SwarmError

    queue = FakeQueue()

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        response = json.loads(done.stdout)
        raw = response["data"].get("resource")
        if raw and "baseRefName" in raw:
            raw["baseRefName"] = base
            if not queue_exists:
                raw["repository"]["mergeQueue"] = None
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    with pytest.raises(SwarmError):
        merge_queue.prioritize(URL, run=run)
    assert queue.mutations == []


def test_priority_reenqueues_a_critical_entry_ejected_during_reorder():
    queue = FakeQueue(queued=True)

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        if "dequeuePullRequest" in " ".join(command) and "id=P2" in command:
            queue.entries = [e for e in queue.entries if e["pullRequest"]["id"] != "P9"]
        return done

    result = merge_queue.prioritize(URL, run=run)
    assert result["errors"] == []
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P4", "P9", "P2", "P3"]


def test_priority_attempts_every_restoration_after_one_is_refused():
    queue = FakeQueue()

    def run(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P2" in command:
            return subprocess.CompletedProcess(command, 1, "", "restore refused")
        return queue.run(command, **kwargs)

    result = merge_queue.prioritize(URL, run=run)
    assert result["displaced"] == [URL[:-1] + "3"]
    assert "could not restore" in result["errors"][0]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P9", "P3"]
