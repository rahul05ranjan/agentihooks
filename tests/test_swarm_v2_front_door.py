import json

import pytest

import scripts.swarm_ledger.repository  # noqa: F401  puts the ledger folder on sys.path before ledger_core loads
from scripts.gates import log, talk
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_server
from scripts.swarm_v2 import front_door, master_fence
from tests import sv2_mst04_cases as cases
from tests.sv2_mst04_cases import ENGINEER, LEAD, LEAD_NAME, SECOND, SECOND_NAME, SLUG

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SEATLESS = "master@abcdef-0009"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "ledgers")
    core.LEDGER_DIR.mkdir()
    return cases.World()


def write(world, op, connect=None, home=None):
    door = front_door.Door(SLUG, connect=connect or (lambda: world.redis), home=home)
    state, rejected = core.sync(SLUG, ops=[op], gate=door)
    return op["id"] not in rejected, state["_meta"]["warnings"]


def chat(by, kind="add", n=0, text="hello from the masters"):
    return {"op": kind, "thread": "chat", "id": f"m-{n}", "by": by, "text": text, "to": "operator"}


def chat_texts(world):
    return [(entry["by"], entry["text"]) for entry in world.doc()["chat"]]


def test_the_front_door_is_the_bare_master_seat():
    assert front_door.seat("sw") == "master@sw"
    assert [front_door.holds("sw", held) for held in ("master@sw", "", "master-2@sw", "master@other")] == [
        True,
        True,
        False,
        False,
    ]


def test_the_refusal_names_the_front_door_and_the_masters_channel():
    assert front_door.refusal("sw", "master-2@sw") == (
        "page chat belongs to the front door master master@sw, so master-2@sw cannot write it; ask the front door "
        'with agentihooks swarm sw masters-channel say "<text>"'
    )


@pytest.mark.parametrize(
    ("op", "covered"),
    [
        ({"op": "add", "thread": "chat", "by": "m"}, True),
        ({"op": "edit", "thread": "chat", "by": "m"}, True),
        ({"op": "add", "thread": "chat"}, False),
        ({"op": "delete", "thread": "chat", "by": "m"}, False),
        ({"op": "clear", "thread": "chat", "by": "m"}, False),
        ({"op": "add", "thread": "followups/f1/comments", "by": "m"}, False),
        ({"op": "answer", "item": "questions/q1", "by": "m"}, False),
    ],
)
def test_only_agent_chat_adds_and_edits_are_covered(op, covered):
    assert front_door.chat_write(op) is covered


def test_a_backend_master_chat_write_is_refused_naming_the_front_door(world):
    accepted, warnings = write(world, chat(SECOND_NAME))

    assert accepted is False
    assert warnings == [front_door.refusal(SLUG, SECOND)]
    assert LEAD in warnings[0]
    assert chat_texts(world) == []


def test_a_backend_master_chat_edit_is_refused(world):
    assert write(world, chat(LEAD_NAME))[0] is True

    accepted, warnings = write(world, chat(SECOND_NAME, kind="edit", text="changed by master two"))

    assert (accepted, warnings) == (False, [front_door.refusal(SLUG, SECOND)])
    assert chat_texts(world) == [(LEAD_NAME, "hello from the masters")]


@pytest.mark.parametrize("by", [LEAD_NAME, SEATLESS, ENGINEER])
def test_the_front_door_a_seatless_master_and_other_lanes_write_chat(world, by):
    assert write(world, chat(by)) == (True, [])
    assert chat_texts(world) == [(by, "hello from the masters")]


def test_a_backend_master_still_comments_on_items(world):
    op = {"op": "add", "thread": f"{cases.FOLLOWUP}/comments", "id": "c-1", "by": SECOND_NAME, "text": "on it now"}

    assert write(world, op) == (True, [])


def test_other_writes_never_reach_redis(world):
    def unreachable():
        raise AssertionError("the front door connected to Redis for a write it does not cover")

    assert write(world, chat(ENGINEER), connect=unreachable) == (True, [])


def test_a_crashed_seat_lookup_lets_the_write_through_and_logs_it(world, tmp_path):
    def broken():
        raise ConnectionError("redis down")

    assert write(world, chat(SECOND_NAME), connect=broken, home=tmp_path) == (True, [])
    rows = [json.loads(line) for line in log.gate_log_path(SLUG, tmp_path).read_text().splitlines()]
    assert [(r["gate"], r["kind"], r["agent"], r["tool"], r["reason"]) for r in rows] == [
        (front_door.NAME, "fail-open", SECOND_NAME, "ledger", "ConnectionError: redis down")
    ]


def test_the_door_passes_admitted_writes_to_its_inner_gate(world):
    seen = []

    class Inner:
        def apply(self, doc, op, ctx, apply):
            seen.append(op["id"])
            return apply(doc, op, ctx)

    door = front_door.Door(SLUG, Inner(), connect=lambda: world.redis)
    _, rejected = core.sync(SLUG, ops=[chat(LEAD_NAME, n=1), chat(SECOND_NAME, n=2)], gate=door)

    assert (seen, set(rejected)) == (["m-1"], {"m-2"})


def test_the_ledger_write_gate_puts_the_front_door_inside_the_fence_and_outside_the_talk_budget():
    gate = ledger_server.write_gate(SLUG)

    assert isinstance(gate, master_fence.Fence)
    assert isinstance(gate.inner, front_door.Door)
    assert isinstance(gate.inner.inner, talk.Budget)
    assert gate.inner.slug == SLUG
