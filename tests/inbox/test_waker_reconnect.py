import queue
import threading
import time

import pytest

from scripts.inbox import wake, waker
from scripts.inbox.store import InboxStore, now_ms
from scripts.swarm.models import AgentRecord, SwarmConfig
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def backend():
    import fakeredis

    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    return server, client


def test_running_waker_wakes_pending_items_after_redis_returns(backend, monkeypatch):
    server, client = backend
    store, inbox = RedisStore(client), InboxStore(client)
    store.create(SwarmConfig("sw", "/repo", 1, 1, state="running"))
    store.put_agent("sw", AgentRecord("bob", "eng", "t1", pane_id="p1", harness="codex"))
    polling, retrying, restored, stopped = (threading.Event() for _ in range(4))
    prompts, errors, subscriptions = queue.Queue(), queue.Queue(), []
    real_pubsub = client.pubsub
    real_sleep = time.sleep

    class Subscription:
        def __init__(self):
            self.inner = real_pubsub()
            self.closed = False
            subscriptions.append(self)

        def subscribe(self, name):
            self.inner.subscribe(name)

        def get_message(self, timeout):
            if stopped.is_set():
                raise KeyboardInterrupt
            polling.set()
            return self.inner.get_message(timeout=timeout)

        def close(self):
            self.closed = True
            self.inner.close()

    class Herdr:
        def agent_status(self, agent):
            return "idle"

        def typed_input(self, agent):
            return ""

        def prompt(self, agent, text):
            prompts.put((agent.pane_id, text))

    def sleep(delay):
        if delay >= 1:
            retrying.set()
            assert restored.wait(3), "Redis recovery was never released"
        else:
            real_sleep(delay)

    def run():
        try:
            waker.run(store, Herdr(), now_ms, {})
        except KeyboardInterrupt:
            pass
        except BaseException as exc:
            errors.put(exc)

    monkeypatch.setattr(client, "pubsub", Subscription)
    monkeypatch.setattr(waker, "RECHECK_S", 0.02)
    monkeypatch.setattr(time, "sleep", sleep)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert polling.wait(3), "waker never subscribed"
        inbox.send("alice", "bob", "before outage")
        assert prompts.get(timeout=3) == ("p1", wake.WAKE_TEXT)
        server.connected = False
        assert retrying.wait(3), "waker died instead of retrying"
        assert worker.is_alive()
        server.connected = True
        inbox.send("alice", "bob", "missed during outage")
        restored.set()
        assert prompts.get(timeout=3) == ("p1", wake.WAKE_TEXT)
    finally:
        server.connected = True
        stopped.set()
        restored.set()
        worker.join(3)
    assert not worker.is_alive(), "waker did not stop"
    assert errors.empty(), list(errors.queue)
    assert all(subscription.closed for subscription in subscriptions)
