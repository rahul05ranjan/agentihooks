import io
import urllib.error
from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client


def served(monkeypatch, answer):
    reads = []

    def resource(slug, path, service, collection):
        reads.append((slug, path, service, collection))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(resource=resource))
    return reads


def refused(code):
    return urllib.error.HTTPError("http://ledger", code, "refused", {}, io.BytesIO(b"{}"))


def test_a_task_read_names_one_task_and_returns_its_row(monkeypatch):
    reads = served(monkeypatch, {"id": "t2", "state": "open"})
    assert ledger_client.LedgerClient(service=True).task("demo", "t2") == {"id": "t2", "state": "open"}
    assert reads == [("demo", "tasks/t2", True, False)]


def test_a_task_the_ledger_does_not_hold_reads_as_none(monkeypatch):
    served(monkeypatch, refused(404))
    assert ledger_client.LedgerClient().task("demo", "t9") is None


def test_any_other_refusal_of_a_task_read_is_raised(monkeypatch):
    served(monkeypatch, refused(403))
    with pytest.raises(urllib.error.HTTPError) as raised:
        ledger_client.LedgerClient().task("demo", "t2")
    assert raised.value.code == 403
