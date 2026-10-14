import math

import pytest

from scripts.swarm_ledger.events import patch


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"nested": [True]}, {"nested": [1]}),
        ({"nested": [1]}, {"nested": [1.0]}),
        ([{"value": False}], [{"value": 0}]),
        ([{"value": 0}], [{"value": 0.0}]),
    ],
)
def test_nested_number_type_changes_remain_visible(old, new):
    change = patch.diff(old, new)
    assert change is not None
    result = patch.apply(old, change)
    assert patch.same(result, new)
    assert not patch.same(result, old)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"first": [0.0], "last": None}, {"last": None, "first": [-0.0]}),
        ([{"value": -0.0}], [{"value": 0.0}]),
        ({"first": 1, "last": ["evidence"]}, {"last": ["evidence"], "first": 1}),
    ],
)
def test_equal_json_values_do_not_publish_a_patch(old, new):
    assert patch.diff(old, new) is None


def test_nonfinite_nested_values_keep_their_comparison_behavior():
    value = {"nested": [math.nan]}
    assert not patch.same(value, value)
    assert patch.diff(value, value) is not None
