import json
import threading


class Publishing:
    """A LedgerRepository that hands each stored generation it reads or writes to every publisher(slug, state) once."""

    def __init__(self, inner, *publishers):
        self.inner = inner
        self.publishers = list(publishers)
        self.published = {}
        self.lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def publish(self, slug, state):
        for publisher in self.publishers:
            publisher(slug, state)

    def current(self, slug: str):
        entry = self.inner.current(slug)
        with self.lock:
            if entry.generation > self.published.get(slug, -1):
                self.published[slug] = entry.generation
                self.publish(slug, entry.state)
        return entry

    def get_document(self, slug: str) -> dict:
        return json.loads(self.current(slug).text)

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]:
        state, rejected = self.inner.apply_ops(slug, changes=changes, ops=ops, gate=gate)
        self.current(slug)
        return state, rejected


SHARED = {}


def publishing(inner, publisher):
    """The one wrapper of a repository, shared by every module copy of the server that adds its publisher."""
    wrapper = SHARED.setdefault(id(inner), Publishing(inner))
    if publisher not in wrapper.publishers:
        wrapper.publishers.append(publisher)
    return wrapper
