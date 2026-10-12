import json

import pytest

from scripts.swarm import controller
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2 import deployed, master_fill
from scripts.swarm_v2.kubernetes import spec
from scripts.swarm_v2.masters import MasterSeats
from scripts.swarm_v2.runtime.base import LOCAL
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from tests.swarm.test_tick import FakeRuntime
from tests.test_swarm_v2_deployed_runtime import ACCOUNT, IMAGE, SLUG, Pods, _cs, _environ, _store

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = _store()
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda environ, namespace: pods)
    monkeypatch.setattr(deployed.image_tag, "resolve", lambda repository, tag: IMAGE)
    seats = MasterSeats(store.redis)
    seats.set_count(SLUG, 2)
    local = FakeRuntime()

    def start(seat=None):
        if seat:
            seats.own_hive(SLUG, "anton", f"{seat}@{SLUG}")
        service = _cs().host(_environ(tmp_path, store), store, "anton")
        runtime = service.runtime
        runtime.herdr_runtime = local
        runtime.router.runtimes[LOCAL] = LocalHerdrRuntime(local)
        runtime.launch.func.__self__.planned = lambda task: None
        return service, runtime

    yield store, pods, local, start


@pytest.mark.parametrize("seat", ["master", "master-2"])
def test_configured_master_seat_runs_as_a_native_master_pod(world, seat):
    from scripts.swarm import master_launch

    store, pods, local, start = world
    service, inner = start(seat)
    runtime = controller.FencedRuntime(store, SLUG, service.controller.held, inner, "distributed")
    try:
        if seat == "master":
            launched = master_launch.up(store, SLUG, runtime, 100, "new", None, None)
            assert launched.seat == f"master@{SLUG}"
        else:
            lead = store.next_name(SLUG, "master")
            local.live.add(lead)
            store.put_agent(SLUG, AgentRecord(lead, "master", "master", seat=f"master@{SLUG}"))
            actions = master_fill.run(SLUG, store.config(SLUG), store, runtime, {}, 100)
            assert len(actions) == 1 and "spawned" in actions[0]
        assert local.masters == []
        [pod] = pods.created
        execution_id = pod["metadata"]["labels"][spec.EXECUTION_LABEL]
        execution = store.execution(SLUG, execution_id)
        assert (execution.lane, execution.task, execution.seat, execution.profile) == (
            "master",
            seat,
            f"{seat}@{SLUG}",
            "master",
        )
        assert (execution.runtime_backend, execution.account, execution.generation) == ("kubernetes", ACCOUNT, 1)
        launch = json.loads(pods.maps[0]["data"]["launch.json"])
        assert launch["profile"] == "master"
        assert launch["authority"]["task_id"] == seat
        assert launch["authority"]["seat_id"] == f"{seat}@{SLUG}"
        assert "master" in launch["prompt"]
        assert pod["metadata"]["annotations"][spec.DOMAIN + "/profile"] == "general"
    finally:
        service.stop()


@pytest.mark.parametrize("seat", ["master", "master-2"])
def test_unconfigured_master_seats_keep_local_placement(world, seat):
    store, pods, local, start = world
    service, runtime = start()
    try:
        task = {"id": seat, "seat": f"{seat}@{SLUG}"}
        placed = runtime.spawn(store.config(SLUG), "master", store.next_name(SLUG, "master"), task)
        assert placed.placement == ""
        assert len(local.masters) == 1
        assert pods.created == []
    finally:
        service.stop()


def test_a_master_owning_another_hive_stays_off_this_controllers_pods(world):
    store, pods, local, start = world
    MasterSeats(store.redis).own_hive(SLUG, "workstation", f"master@{SLUG}")
    service, runtime = start("master-2")
    try:
        assert runtime.router.placed_backend("master", {"id": "master"}) == "local"
        assert runtime.router.placed_backend("master", {"id": "master-2"}) == "kubernetes"
    finally:
        service.stop()
