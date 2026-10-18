import os
from collections.abc import Iterator
from contextlib import contextmanager

_current = None


def _clear() -> None:
    global _current
    _current = None


def secret() -> str | None:
    return _current or os.environ.get("SWARM_SECRET")


@contextmanager
def bound(value: str | None) -> Iterator[None]:
    global _current
    previous = _current
    os.environ.pop("SWARM_SECRET", None)
    _current = value
    try:
        yield
    finally:
        _current = previous


os.register_at_fork(after_in_child=_clear)
