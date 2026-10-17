from threading import Event
from unittest.mock import Mock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.mark.parametrize("lose_ownership", [False, True])
def test_dispatch_lease_renews_until_stopped_and_joins_before_release(monkeypatch, lose_ownership):
    from redis.exceptions import LockError

    from scripts.scheduler import lease

    stopped = Mock(spec=Event)
    stopped.wait.side_effect = [False, True]
    owner = Mock()
    owner.acquire.return_value = True
    if lose_ownership:
        owner.reacquire.side_effect = LockError("ownership lost")
    redis = Mock()
    redis.lock.return_value = owner
    joined = []

    class InlineThread:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args
            assert daemon is True

        def start(self):
            self.target(*self.args)

        def join(self):
            joined.append(True)

    monkeypatch.setattr(lease, "Event", lambda: stopped)
    monkeypatch.setattr(lease, "Thread", InlineThread)

    def release():
        assert joined == [True]
        stopped.set.assert_called_once_with()

    owner.release.side_effect = release
    with lease.dispatch_lock(redis, "proof-lock") as held:
        assert held is owner
    redis.lock.assert_called_once_with("proof-lock", timeout=30, blocking=False, thread_local=False)
    owner.reacquire.assert_called_once_with()
    assert stopped.wait.call_args_list[0].args == (10,)
    owner.release.assert_called_once_with()


@pytest.mark.parametrize("decode", [False, True])
def test_fenced_dispatch_accepts_both_redis_response_encodings(decode):
    import fakeredis

    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(fakeredis.FakeRedis(decode_responses=decode), machine="proof")
    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=0)
    timer.start()
    assert scheduler.run_due(now=1) == 1
    assert timer.state == "fired"
