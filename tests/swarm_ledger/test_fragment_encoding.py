import json

import pytest

from scripts.swarm_ledger.repository.fragments import document
from scripts.swarm_ledger.repository.rows import encode


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"_meta": {"events": [], "stamps": {}}, "tasks": []},
        {"tasks": [{"id": "é𝄞", "a": True, "b": 1, "c": 1.0, "d": -0.0, "text": '\n"\\'}]},
        {"_meta": {"events": [{"rev": 1}, {"rev": 1}], "stamps": {"a": {"rev": 1, "at": 2, "by": "writer"}}}},
        {"_meta": {"events": "scalar", "stamps": []}, "unknown": [None, [], {}, "é", 1.0]},
    ],
)
def test_generation_text_matches_canonical_encoding(state):
    first = document(state)
    second = document(json.loads(encode(state)), first)
    assert first.text == second.text == encode(state)
    assert first.value == state
    assert second is first


def test_generation_reuses_unchanged_rows_and_exact_number_types():
    old = document({"tasks": [{"id": "first", "value": True}, {"id": "second", "value": 0.0}]})
    new = document({"tasks": [{"id": "first", "value": 1}, {"id": "second", "value": 0.0}]}, old)
    assert type(new.value["tasks"][0]["value"]) is int
    assert new.value["tasks"][0] is not old.value["tasks"][0]
    assert new.value["tasks"][1] is old.value["tasks"][1]
    assert new.text == encode(new.value)
    assert type(old.value["tasks"][0]["value"]) is bool


def test_generation_removal_and_reordering_do_not_keep_obsolete_fragments():
    first = document({"title": "first", "tasks": [{"id": "one"}, {"id": "two"}], "_meta": {"stamps": {"a": 1}}})
    changed = {"_meta": {"stamps": {}}, "tasks": [{"id": "two"}, {"id": "one"}, {"id": "three"}]}
    second = document(changed, first)
    third = document({"tasks": []}, second)
    assert second.text == encode(changed)
    assert third.text == encode({"tasks": []})
    assert first.value["_meta"]["stamps"] == {"a": 1}


def test_generation_rejects_nonfinite_values_before_identity_reuse():
    with pytest.raises(ValueError):
        document({"tasks": [{"value": float("nan")}]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_native_null_encoding_cannot_reuse_a_finite_fragment(value):
    previous = document({"tasks": [{"value": None}]})
    with pytest.raises(ValueError):
        document({"tasks": [{"value": value}]}, previous)


def test_large_integers_keep_canonical_precision_in_the_fallback():
    first = document({"tasks": [{"value": 2**100}]})
    same = document({"tasks": [{"value": 2**100}]}, first)
    changed = document({"tasks": [{"value": 2**100 + 1}]}, same)
    assert same is first
    assert changed.text == encode(changed.value)
    assert changed.value["tasks"][0]["value"] == 2**100 + 1
