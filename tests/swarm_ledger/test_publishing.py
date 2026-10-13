import json
from types import SimpleNamespace

from scripts.swarm_ledger.events.publishing import Publishing


class Store:
    def __init__(self):
        self.calls = []
        self.generation = 0

    def current(self, slug):
        self.calls.append(("current", slug))
        state = {"_meta": {"rev": self.generation + 1}, "read": slug}
        return SimpleNamespace(generation=self.generation, text=json.dumps(state), state=state)

    def apply_ops(self, slug, changes=None, ops=None, gate=None):
        self.calls.append(("apply", slug, changes, ops, gate))
        self.generation += 1
        return {"_meta": {"rev": 2}}, ["refused"]

    def exists(self, slug):
        return slug == "here"


def wrapped():
    store, published = Store(), []
    return store, published, Publishing(store, lambda slug, state: published.append((slug, state)))


def test_a_read_returns_the_stored_document_and_publishes_each_generation_once():
    store, published, repository = wrapped()
    assert repository.get_document("s") == {"_meta": {"rev": 1}, "read": "s"}
    assert repository.get_document("s") == {"_meta": {"rev": 1}, "read": "s"}
    assert store.calls == [("current", "s"), ("current", "s")]
    assert published == [("s", {"_meta": {"rev": 1}, "read": "s"})]
    assert repository.current("t").state == {"_meta": {"rev": 1}, "read": "t"}
    store.generation = 4
    repository.current("s")
    store.generation = 3
    repository.current("s")
    assert published == [
        ("s", {"_meta": {"rev": 1}, "read": "s"}),
        ("t", {"_meta": {"rev": 1}, "read": "t"}),
        ("s", {"_meta": {"rev": 5}, "read": "s"}),
    ]


def test_a_write_returns_state_and_refusals_and_publishes_the_stored_generation():
    store, published, repository = wrapped()
    assert repository.apply_ops("s", changes=["c"], ops=["o"], gate="g") == ({"_meta": {"rev": 2}}, ["refused"])
    assert store.calls == [("apply", "s", ["c"], ["o"], "g"), ("current", "s")]
    assert published == [("s", {"_meta": {"rev": 2}, "read": "s"})]


def test_every_other_repository_call_passes_through_unpublished():
    _, published, repository = wrapped()
    assert repository.exists("here") and not repository.exists("gone")
    assert published == []


def test_module_copies_of_the_server_share_one_wrapper_per_repository():
    from scripts.swarm_ledger.events.publishing import publishing

    store, seen = Store(), []
    first = publishing(store, lambda slug, state: seen.append(("first", slug)))
    second = publishing(store, lambda slug, state: seen.append(("second", slug)))
    assert first is second
    publishing(store, first.publishers[0])
    assert len(first.publishers) == 2
    first.get_document("s")
    assert seen == [("first", "s"), ("second", "s")]
    assert publishing(Store(), lambda slug, state: None) is not first
