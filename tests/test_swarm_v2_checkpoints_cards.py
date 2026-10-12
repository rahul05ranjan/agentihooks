from dataclasses import asdict

import pytest

from scripts.swarm.status import handoff_rows
from scripts.swarm_v2.checkpoints import cards

pytestmark = pytest.mark.unit


@pytest.fixture
def world():
    from tests import sv2_kub05_cases as cases

    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.live = controller
        world.old = world.launch(controller, world.fx["first"])
        yield world


@pytest.mark.parametrize("continuation,label", [("native", "Resume native"), ("context", "Continue from context")])
def test_a_real_attempt_failure_card_offers_its_verified_checkpoint(world, continuation, label):
    recovery = world.recovery(world.live, automatic=False)
    world.checkpoints.by_execution[world.old.execution_id] = [
        {
            "checkpoint_id": "ckp-verified",
            "sequence": 1,
            "status": "complete",
            "compatibility": world.fx["compatibility"],
            "continuation": continuation,
            "watermark": 41,
        }
    ]
    recovery.handle(world.old.execution_id, "node_lost")
    source = cards.rows(world.store, recovery.slug)
    [row] = handoff_rows([], [asdict(world.occupant())], source)
    assert row["awaiting"] == world.old.name
    assert row["resume_available"] is True
    assert row["resume_label"] == label
    assert row["checkpoint"] == "ckp-verified"
    assert row["reason"].endswith("at record 41")
    assert world.store.restored(recovery.slug) == []


def test_a_card_with_no_checkpoint_offers_only_fresh_without_rewriting_legacy_restore_rows(world):
    recovery = world.recovery(world.live, automatic=False)
    world.checkpoints.by_execution[world.old.execution_id] = []
    world.store.put_restored(recovery.slug, [{"name": "legacy", "outcome": "awaiting-decision"}])
    recovery.handle(world.old.execution_id, "node_lost")
    [row] = handoff_rows([], [asdict(world.occupant())], cards.rows(world.store, recovery.slug))
    assert row["resume_available"] is False
    assert row["checkpoint"] == ""
    assert world.store.restored(recovery.slug) == [{"name": "legacy", "outcome": "awaiting-decision"}]
