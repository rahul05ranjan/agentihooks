import copy

import pytest

from scripts.swarm_ledger.repository import fragments, rows


@pytest.mark.parametrize("value", [{"value": 1e20}, {"value": 1e-7}, {"text": "x" * 100}])
def test_leaf_reuses_the_committed_signature_for_unchanged_rows(value):
    previous = fragments.leaf(value)
    current = fragments.leaf(copy.deepcopy(value), previous)
    assert current is previous
    assert current.text == rows.encode(value)


def test_equal_native_and_canonical_text_share_one_retained_string():
    fragment = fragments.leaf({"text": "x" * 100})
    assert fragment.signature is fragment.text
