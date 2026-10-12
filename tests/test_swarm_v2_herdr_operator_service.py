import pytest

from scripts.swarm_v2.herdr.endpoints import Endpoint, Scope
from tests import sv2_hdr03_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
FX = cases.fixture()
HOST_KEY = cases.HOST_KEY
SWARMS = ("hive-a", "hive-b")


def store():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug in SWARMS:
        made.create(SwarmConfig(slug, "agentihooks", 1, 0))
    return made


def worker(made, slug: str, backend: str = "kubernetes") -> tuple[str, int]:
    from scripts.swarm.store import AgentRecord

    seat = len(made.execution_occupants(slug)) + 1
    agent = AgentRecord(
        made.next_name(slug, "eng"),
        "eng",
        "task",
        seat=f"eng-{seat}@{slug}",
        runtime_backend=backend,
        runtime_target=(
            {"pod_namespace": FX["namespace"], "pod_name": "attempt"}
            if backend == "kubernetes"
            else {"server_id": "local", "pid": 1}
        ),
    )
    admitted = made.start_execution(slug, agent)
    return admitted.execution_id, admitted.generation


def publish(made, slug: str, execution_id: str, generation: int) -> None:
    from scripts.swarm_v2.kubernetes.terminal import HostKeys

    HostKeys(made).record(Scope("controller", slug, execution_id, generation), HOST_KEY)


def end(made, slug: str, execution_id: str) -> None:
    from scripts.swarm_v2.kubernetes.terminal import HostKeys

    HostKeys(made).forget(slug, execution_id)


def name(slug: str, execution_id: str, generation: int) -> str:
    return Endpoint(Scope("operator/operator", slug, execution_id, generation), FX["namespace"]).name


class Herdr(cases.Herdr):
    def __init__(self) -> None:
        super().__init__([FX["manual"]], set())

    def _add(self, command: list[str]):
        self.ready.add(command[-1])
        return super()._add(command)


def service(tmp_path, made, herdr):
    from scripts.swarm_v2.herdr.operator_service import Service
    from scripts.swarm_v2.herdr.upkeep import Upkeep

    terminal = cases.client_terminal(tmp_path / "client", herdr, FX["namespace"])
    return Service(Upkeep(terminal), lambda: made)


def test_a_pass_saves_the_published_workers_of_every_hive_into_the_operator_herdr(tmp_path):
    made, herdr = store(), Herdr()
    first, second = worker(made, "hive-a"), worker(made, "hive-b")
    publish(made, "hive-a", *first)
    publish(made, "hive-b", *second)

    report = service(tmp_path, made, herdr).run_pass()

    assert sorted(report.registered) == sorted([first[0], second[0]])
    assert report.refused == {}
    assert cases._sidebar(herdr) == ["Local", "devbox", name("hive-a", *first), name("hive-b", *second)]


def test_a_worker_that_ends_is_removed_on_the_next_pass(tmp_path):
    made, herdr = store(), Herdr()
    first, second = worker(made, "hive-a"), worker(made, "hive-a")
    publish(made, "hive-a", *first)
    publish(made, "hive-a", *second)
    running = service(tmp_path, made, herdr)
    running.run_pass()

    end(made, "hive-a", first[0])
    report = running.run_pass()

    assert list(report.registered) == [second[0]]
    assert report.retired == [first[0]]
    assert cases._sidebar(herdr) == ["Local", "devbox", name("hive-a", *second)]


def test_an_unchanged_hive_runs_no_herdr_command(tmp_path):
    made, herdr = store(), Herdr()
    publish(made, "hive-a", *worker(made, "hive-a"))
    running = service(tmp_path, made, herdr)
    running.run_pass()
    ran = len(herdr.commands)

    assert running.run_pass() is None
    assert len(herdr.commands) == ran


def test_only_kubernetes_workers_with_a_published_host_key_of_their_generation_are_saved(tmp_path):
    made, herdr = store(), Herdr()
    kept = worker(made, "hive-a")
    publish(made, "hive-a", *kept)
    worker(made, "hive-a")
    local = worker(made, "hive-a", backend="local")
    publish(made, "hive-a", *local)
    stale = worker(made, "hive-b")
    publish(made, "hive-b", stale[0], stale[1] + 1)

    report = service(tmp_path, made, herdr).run_pass()

    assert list(report.registered) == [kept[0]]


def test_a_refused_transport_saves_and_removes_nothing(tmp_path):
    from scripts.swarm_v2.herdr.operator_service import TRANSPORT, Service
    from scripts.swarm_v2.herdr.publications import TransportRefused
    from scripts.swarm_v2.herdr.upkeep import Upkeep

    herdr = Herdr()

    def refuse():
        raise TransportRefused("the private swarm Redis at swarm.example refused the connection: ConnectionError")

    terminal = cases.client_terminal(tmp_path / "client", herdr, FX["namespace"])
    report = Service(Upkeep(terminal), refuse).run_pass()

    assert report.refused == {
        TRANSPORT: "the private swarm Redis at swarm.example refused the connection: ConnectionError"
    }
    assert herdr.commands == []
