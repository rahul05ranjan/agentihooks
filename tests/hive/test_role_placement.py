import pytest

from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive

ALL_ROLES = ["master", "qa", "frontend", "eng", "ci", "plan"]


def two_hosts(setup, anton_agents=0):
    from scripts.hive import registry

    setup.store.redis.delete(registry.key("remote"))
    setup.store.redis.srem(registry.INDEX, "remote")
    write_hive(setup, id="workstation", ui="yes", roles=ALL_ROLES, max_agents=10)
    write_hive(setup, id="anton", ui="no", roles=["eng", "ci", "plan"], max_agents=2)
    setup.store.redis.set(f"{registry.key('anton')}:sessions", f'{{"eng": {anton_agents}}}')


ROLES = {
    "frontend engineer": ("eng", "frontend", {"id": "task"}, "workstation"),
    "qa agent": ("eng", "qa", {"id": "task"}, "workstation"),
    "master": ("master", "master", {"id": "master"}, "workstation"),
    "engineer": ("eng", "engineer", {"id": "task"}, "anton"),
    "planner": ("plan", "planner", {"id": "task"}, "anton"),
}


@pytest.mark.parametrize(("lane", "profile", "task", "host"), ROLES.values(), ids=ROLES.keys())
def test_each_role_lands_on_its_host_kind(setup, lane, profile, task, host):
    from scripts.hive.launch import Selection
    from scripts.swarm.profile_choice import ProfileDecision

    two_hosts(setup)
    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    selected = setup.launcher.select(setup.config, lane, task)
    assert isinstance(selected, Selection)
    assert selected.hive == host
    assert selected.fallback == ""


def test_a_full_headless_fleet_falls_back_to_the_workstation_with_the_reason_recorded(setup):
    from scripts.swarm_v2.runtime.base import SpawnRequest

    two_hosts(setup, anton_agents=2)
    task = {"id": "task", "seat": "eng-1@fixture", "controller_epoch": setup.held.epoch}
    selected = setup.launcher.select(setup.config, "eng", task)
    assert selected.hive == "workstation"
    assert selected.fallback == "no headless hive for eng has room"
    name = setup.store.next_name(setup.config.slug, "eng")
    admitted = setup.launcher.admit(SpawnRequest(setup.config, "eng", name, task), selected)
    assert admitted.profile_decision["placement_fallback"] == "no headless hive for eng has room"


def test_the_hive_controller_routes_the_plan_lane_through_hive_placement(setup):
    from scripts.swarm import controller
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime, hive_controller

    two_hosts(setup)
    routed = hive_controller(RoutedRuntime(setup.herdr, RuntimeRouter([])), setup.store, setup.config.slug, setup.held)
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")
    assert fenced.runtime.router.placed_backend("plan", {"id": "task"}) == "hive"
    assert fenced.placement_refusal("plan", {"id": "task"}) == ""
