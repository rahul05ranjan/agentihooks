import pytest

from scripts.swarm_v2 import retention
from tests import sv2_kub04_cases as cases
from tests.test_swarm_v2_kubernetes_cleanup import ready as ready


def refused(world, old):
    name = f"swarm-{old.execution_id}"
    world.api.objects[name]["status"] = {
        "phase": "Failed",
        "reason": "Error",
        "containerStatuses": [
            {
                "name": "agent",
                "state": {"terminated": {"exitCode": 78, "reason": "Error", "message": "startup refused"}},
            }
        ],
    }
    return name


def test_refused_startup_log_and_exit_reason_remain_readable_after_cleanup(ready):
    world, controller, old, _ = ready
    name = refused(world, old)

    def logs(pod, container):
        assert pod == name and container == "agent"
        assert name in world.api.objects
        return "worker startup refused: profile binding mismatch\n"

    world.api.pod_logs = logs
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert name not in world.api.objects
    kept = retention.retained(world.store, cases.SLUG, old.execution_id)
    startup = kept["removed"]["pods"][0]["startup"]
    assert startup["phase"] == "Failed"
    assert startup["reason"] == "Error"
    assert startup["containers"] == [
        {
            "name": "agent",
            "terminated": {"exitCode": 78, "reason": "Error", "message": "startup refused"},
            "log": "worker startup refused: profile binding mismatch\n",
        }
    ]


def test_unreadable_startup_logs_keep_the_pod_for_retry(ready):
    world, controller, old, _ = ready
    name = refused(world, old)

    def unavailable(pod, container):
        raise ConnectionError("logs unavailable")

    world.api.pod_logs = unavailable
    with pytest.raises(ConnectionError, match="logs unavailable"):
        world.cleanup(controller).run(old.execution_id)
    assert name in world.api.objects
    assert retention.retained(world.store, cases.SLUG, old.execution_id) is None
