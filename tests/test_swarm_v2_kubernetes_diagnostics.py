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


def test_logs_are_persisted_before_delete_and_reused_after_a_refused_delete(ready):
    import json

    world, controller, old, _ = ready
    name = refused(world, old)
    world.api.pod_logs = lambda pod, container: "startup refused\n"
    delete = world.api.delete

    def interrupted(kind, pod, uid):
        journal = json.loads(world.store.redis.hget(world.store.key(cases.SLUG, "cleanup-journal"), old.execution_id))
        assert journal["pinned"]["pods"][0]["startup"]["containers"][0]["log"] == "startup refused\n"
        raise ConnectionError("delete unavailable")

    world.api.delete = interrupted
    with pytest.raises(ConnectionError, match="delete unavailable"):
        world.cleanup(controller).run(old.execution_id)
    assert name in world.api.objects
    world.api.delete = delete

    def unreadable(pod, container):
        raise AssertionError("persisted logs must not be fetched twice")

    world.api.pod_logs = unreadable
    assert world.cleanup(controller).run(old.execution_id).state == "done"
    assert retention.retained(world.store, cases.SLUG, old.execution_id)["removed"]["pods"][0]["startup"]


@pytest.mark.parametrize("status", [200, 403, 404])
def test_pod_log_requests_are_bounded_and_refusals_raise(status):
    from urllib.parse import parse_qs, urlsplit

    from scripts.swarm_v2.kubernetes.client import ApiRefused, PodClient

    class Http:
        def send(self, method, path, body=None, read=None):
            assert method == "GET" and body is None
            parsed = urlsplit(path)
            assert parsed.path == "/api/v1/namespaces/workers/pods/refused/log"
            assert parse_qs(parsed.query) == {"container": ["agent"], "limitBytes": ["65536"], "timestamps": ["true"]}
            return (status, read(b"startup refused\xff\n")) if status == 200 else (status, {})

    client = PodClient(Http(), "workers")
    if status == 200:
        assert client.pod_logs("refused", "agent") == "startup refused\ufffd\n"
    else:
        with pytest.raises(ApiRefused):
            client.pod_logs("refused", "agent")
