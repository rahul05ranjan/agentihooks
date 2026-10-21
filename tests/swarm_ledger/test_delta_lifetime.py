import gc
import weakref

import pytest

from scripts.swarm_ledger.repository import rows


@pytest.mark.parametrize("malformed", [False, True])
def test_delta_rows_are_released_without_cyclic_collection(monkeypatch, malformed):
    class Encoded(str):
        pass

    encode = rows.encode
    references = []

    def tracked(value):
        encoded = Encoded(encode(value))
        references.append(weakref.ref(encoded))
        return encoded

    monkeypatch.setattr(rows, "encode", tracked)
    enabled = gc.isenabled()
    gc.disable()
    try:
        if malformed:
            with pytest.raises(TypeError):
                rows.diff({"title": "old"}, {"title": "new", "invalid": {object()}})
        else:
            before, after = rows.diff({"title": "old"}, {"title": "new"})
            assert before['["title"]'][5] == '"old"'
            assert after['["title"]'][5] == '"new"'
            del before, after
        assert references
        assert all(reference() is None for reference in references)
    finally:
        if enabled:
            gc.enable()
