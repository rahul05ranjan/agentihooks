import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.hive import daemon, launch, orders
from scripts.swarm import command_runner, commands, prompt, resume
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.unit

TASK = {
    "id": "task",
    "title": "Carry the opening to the hive",
    "description": "launch this task",
    "workspace": "/controller/.agentihooks/swarm/fixture/tasks/task",
}


@pytest.fixture
def homes(tmp_path):
    controller, hive = tmp_path / "controller", tmp_path / "hive"
    controller.mkdir()
    hive.mkdir()
    return SimpleNamespace(controller=controller, hive=hive)


def at(home):
    return patch.object(Path, "home", return_value=home)


@pytest.fixture
def setup(homes):
    import fakeredis

    from scripts.hive.orders import Consumer, LaunchOrder
    from scripts.swarm import lease
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    config = SwarmConfig("fixture", "agentihooks", 1, 0)
    store.create(config)
    held = lease.acquire(store, "fixture", "controller")
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            state="launching",
            harness="claude",
            profile="engineer",
            account="account",
            hive="host",
            runtime_target={"host_id": "host"},
        ),
    )
    order = LaunchOrder(
        "fixture",
        agent.execution_id,
        agent.generation,
        "engineer",
        "eng",
        "task",
        "account",
        "claude",
        "subscription",
        "prompt",
        held.epoch,
        lease.now_ms(store) + 60000,
    )
    placed = Placed(
        "pane",
        "claude",
        "account",
        model="selected-model",
        effort="high",
        profile="engineer",
        profile_decision={"validation": {"pid": 12}},
    )
    seen = {}

    def spawn(request):
        seen["task"] = request.task
        seen["opening"] = prompt.build(
            request.config.slug, request.config.repo, request.lane, request.name, request.task
        )
        return Outcome("spawn", Status.OK, "local", placed)

    runtime = Mock()
    runtime.spawn.side_effect = spawn
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    return SimpleNamespace(
        redis=redis,
        store=store,
        config=config,
        held=held,
        agent=agent,
        order=order,
        runtime=runtime,
        seen=seen,
        consumer=Consumer(redis, "host", runtime),
    )


def carry(setup, homes):
    from scripts.swarm_v2.runtime.base import SpawnRequest

    with at(homes.controller):
        folder = ledger_workspace.scaffold("fixture", TASK)
        (folder / "progress.md").write_text("controller step\n", encoding="utf-8")
        request = SpawnRequest(setup.config, "eng", setup.agent.name, TASK)
        text = orders.opening(request)
        payload = orders.carried("fixture", TASK, text)
    setup.redis.set("prompt", json.dumps(payload))
    return text, payload


def test_the_hive_launches_with_the_identical_prompt_digest(setup, homes):
    text, payload = carry(setup, homes)
    with at(homes.hive):
        assert setup.consumer.apply(setup.order).status == "live"
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert payload[orders.DIGEST] == digest
    assert hashlib.sha256(setup.seen["opening"].encode()).hexdigest() == digest
    assert "~/.agentihooks/swarm/fixture/tasks/task" in text
    assert "/controller" not in text


def test_the_hive_materializes_the_seeds_below_its_own_home(setup, homes):
    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
    folder = homes.hive / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task"
    assert setup.seen["task"]["workspace"] == str(folder)
    assert (folder / "progress.md").read_text(encoding="utf-8") == "controller step\n"
    assert (folder / "steering.md").read_text(encoding="utf-8").startswith("# task: Carry the opening to the hive")
    assert (folder / "proof.md").read_text(encoding="utf-8") == ""


def test_the_hive_keeps_its_own_progress_on_relaunch(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "seed\n"})
        (folder / "progress.md").write_text("hive step\n", encoding="utf-8")
        ledger_workspace.materialize("fixture", "task", {"progress.md": "seed\n", "steering.md": "new\n"})
    assert (folder / "progress.md").read_text(encoding="utf-8") == "hive step\n"
    assert (folder / "steering.md").read_text(encoding="utf-8") == "new\n"


@pytest.mark.parametrize("change", ["opening", "digest"])
def test_a_changed_opening_is_refused_before_launch(setup, homes, change):
    _, payload = carry(setup, homes)
    if change == "opening":
        payload[orders.OPENING] += " and something else"
    else:
        payload[orders.DIGEST] = "0" * 64
    setup.redis.set("prompt", json.dumps(payload))
    with at(homes.hive):
        ack = setup.consumer.apply(setup.order)
    assert (ack.status, ack.failure) == ("failed", "prompt")
    setup.runtime.spawn.assert_not_called()


def test_hive_progress_lines_reach_the_ledger(setup, homes):
    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
        folder = ledger_workspace.folder("fixture", "task")
        with (folder / "progress.md").open("a", encoding="utf-8") as fh:
            fh.write("hive step one\nhive step two\n")
        setup.consumer.sync_workspaces(45)
    assert 0 < setup.redis.ttl(ledger_workspace.synced_key("host")) <= 45
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}, {"id": "other"}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        tails = ledger_workspace.published(setup.store, "fixture", ledger)
        with patch.object(command_runner, "status_report", return_value={}):
            command_runner.publish(setup.store, "fixture", ledger)
    assert tails == {"task": {"latest_progress": "controller step\nhive step one\nhive step two"}}
    assert commands.workspaces(setup.store, "fixture") == tails


def test_a_local_task_reads_its_own_folder(setup, homes):
    carry(setup, homes)
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="host"):
        tails = ledger_workspace.published(setup.store, "fixture", ledger)
    assert tails == {"task": {"latest_progress": "controller step"}}


def test_the_daemon_syncs_the_workspaces_it_hosts(setup, homes, monkeypatch):
    monkeypatch.setattr(daemon, "fence", lambda *args: False)
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {})
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": {}, "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon, "execution_report", lambda *args: {"names": [], "panes": {}, "executions": {}})
    monkeypatch.setattr(orders.Consumer, "local", classmethod(lambda cls, redis, hive: setup.consumer))
    with at(homes.hive):
        ledger_workspace.materialize("fixture", "task", {"progress.md": "hive step\n"})
        daemon.beat(setup.redis, "host")
    synced = json.loads(setup.redis.get(ledger_workspace.synced_key("host")))
    assert synced == {"fixture": {"task": {"latest_progress": "hive step"}}}


def test_the_controller_carries_the_opening_by_redis_key(setup, homes):
    from scripts.hive import registry
    from scripts.swarm.capacity import Account
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_v2.runtime.base import SpawnRequest

    hive = registry._default("host")
    hive.update(
        roles=["eng"],
        slots=[{"harness": "claude", "account": "account", "kind": "subscription"}],
        repos=[setup.config.repo],
        heartbeat_at=orders.lease.now_ms(setup.store),
        max_agents=3,
    )
    setup.redis.hset(registry.key("host"), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    setup.redis.sadd(registry.INDEX, "host")
    herdr = Mock(spec=HerdrRuntime)
    herdr._profile_decision.return_value = ProfileDecision("engineer", "task", "engineer")
    herdr._quota_accounts = [Account("claude", "account", "room", 0, 80, 80, 6)]
    herdr.planned_account.return_value = None
    launcher = launch.HiveLaunch(setup.store, "fixture", setup.held, herdr)
    launcher.opening = lambda request: f"opening for {request.name}"
    launcher.sleep = Mock(side_effect=StopIteration)
    task = {**TASK, "seat": "eng-2@fixture", "controller_epoch": setup.held.epoch}
    with at(homes.controller), pytest.raises(StopIteration):
        launcher.spawn(SpawnRequest(setup.config, "eng", "eng-name", task))
    payload = json.loads(setup.redis.get(launcher.orders_for("task").prompt_ref))
    assert payload[orders.OPENING] == "opening for eng-name"
    assert payload[orders.DIGEST] == hashlib.sha256(b"opening for eng-name").hexdigest()


def test_resume_texts_name_the_remote_folder_below_its_home(setup, homes):
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        restored = resume.restored_text("fixture", setup.agent, "ledger show")
        recovered = resume._recovery_text("fixture", setup.agent, TASK)
    assert "~/.agentihooks/swarm/fixture/tasks/task" in restored
    assert "~/.agentihooks/swarm/fixture/tasks/task" in recovered
    assert "/controller" not in restored + recovered


def test_resume_texts_keep_the_local_folder(setup, homes):
    with at(homes.controller), patch.object(commands, "hive_id", return_value="host"):
        restored = resume.restored_text("fixture", setup.agent, "ledger show")
        recovered = resume._recovery_text("fixture", setup.agent, TASK)
    assert str(homes.controller / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task") in restored
    assert TASK["workspace"] in recovered
