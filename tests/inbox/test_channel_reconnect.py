import pytest

from scripts.inbox import channel
from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def backend():
    import fakeredis

    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    return server, InboxStore(client)


def test_running_channel_rechecks_pending_items_after_redis_returns(backend, monkeypatch):
    import anyio

    from tests.inbox.test_channel_server import _message, session

    server, inbox = backend
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.02)
    real_sleep = anyio.sleep

    async def scenario(send, receive):
        retrying, restored = anyio.Event(), anyio.Event()

        async def sleep(delay):
            if delay >= 1:
                retrying.set()
                await restored.wait()
            else:
                await real_sleep(delay)

        monkeypatch.setattr(anyio, "sleep", sleep)
        first = inbox.send("alice", "bob", "before outage")
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == first.id
        server.connected = False
        with anyio.fail_after(3):
            await retrying.wait()
            await send(_message(id=3, method="tools/list"))
            listed = await receive()
        assert listed["id"] == 3
        server.connected = True
        pending = inbox.send("alice", "bob", "missed during outage")
        restored.set()
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == pending.id

    try:
        session(inbox, scenario)
    finally:
        server.connected = True

def test_channel_opens_while_redis_is_down_and_delivers_after_restore(backend, monkeypatch):
    import anyio
    from redis.exceptions import RedisError

    from scripts.inbox.store import InboxError
    from tests.inbox.test_channel_server import session

    server, inbox = backend
    server.connected = False
    monkeypatch.setattr(channel, "SETTLE_S", 0)
    monkeypatch.setattr(channel, "RECHECK_S", 0.02)

    def connect():
        try:
            inbox.redis.ping()
        except RedisError as exc:
            raise InboxError("Redis is unreachable") from exc
        return inbox

    monkeypatch.setattr(channel, "connect", connect)

    async def scenario(send, receive):
        server.connected = True
        pending = inbox.send("alice", "bob", "queued before reconnect")
        with anyio.fail_after(3):
            shown = await receive()
        assert shown["params"]["meta"]["item_id"] == pending.id

    try:
        session(None, scenario)
    finally:
        server.connected = True


def test_main_keeps_serving_when_redis_is_unavailable(monkeypatch):
    import anyio

    from scripts.inbox.store import InboxError

    def down():
        raise InboxError("Redis is unreachable")

    calls = []
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    monkeypatch.setattr(channel, "connect", down)
    monkeypatch.setattr(anyio, "run", lambda *args: calls.append(args))
    channel.main()
    assert calls == [(channel.serve, None, "bob")]
