import pytest

from scripts.swarm import tick_master

pytestmark = pytest.mark.xdist_group("fakeredis")


def test_a_forced_launch_reports_the_unfinished_lead_that_blocks_replacement():
    from scripts.swarm.store import MASTER, AgentRecord
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="suspect", seat="master@sw")
    swarm, runtime = direct(lead)
    runtime.live.clear()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", config, swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001)
    )

    assert actions == [
        "master down 5 minutes, forced a master launch",
        "master launch refused: unfinished lead master@a1b2c3-0002 (suspect)",
    ]
    assert swarm.agents("sw") == [lead]
    assert runtime.masters == []
