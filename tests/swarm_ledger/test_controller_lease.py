import json
import secrets
import time
import uuid
from urllib.error import HTTPError

import pytest

from scripts.swarm import controller, lease
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_ledger import authority_context, ledger_authority, ledger_route, ledger_server
from scripts.swarm_ledger.api.client import ResourceClient
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")
live = _authority_live


def test_a_transferred_hosted_tick_passes_and_the_old_local_epoch_is_refused(live, monkeypatch):
    import fakeredis

    source = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    hosted = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    base = f"http://{live['host']}"
    monkeypatch.setenv("SWARM_URL", base)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    source.redis.set(source.key(SLUG, "control-epoch"), 40)
    old = lease.acquire(source, SLUG, "workstation")
    hosted.redis.set(hosted.key(SLUG, "control-epoch"), 8)
    secret = secrets.token_hex(32)
    monkeypatch.setattr(ledger_server.authority, "environ", {"SWARM_AUTH_MODE": "edge", "SWARM_SECRET": secret})
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda *_: hosted.redis)
    token = ledger_authority.tick_token(secret, SLUG, int(time.time()) + ledger_authority.TICK_TTL)
    client = ResourceClient(base, {"X-Ledger-Tick": token})
    data = client.request(
        SLUG,
        "controller-lease/actions",
        {"action": "transfer", "owner": old.owner, "source_epoch": old.epoch, "expected_epoch": 0},
    )["data"]
    transferred = lease.Lease(**data)
    assert transferred.epoch > old.epoch
    assert lease.current(hosted, SLUG) == transferred
    assert lease.release(source, SLUG, old)
    assert lease.current(source, SLUG) is None
    with authority_context.bound(secret), ledger_route.scoped(SLUG, base):
        tick = controller.FencedLedger(source, SLUG, transferred, LedgerClient())
        tick.comment_phase(SLUG, "p1", "Transferred tick applied", "swarm")
        state = tick.state(SLUG)
        assert any(comment["text"] == "Transferred tick applied" for comment in state["phases"][0]["comments"])
        stale = controller.FencedLedger(source, SLUG, old, LedgerClient())
        with pytest.raises(SwarmError, match="stale"):
            stale.comment_phase(SLUG, "p1", "Stale tick refused", "swarm")
    operation = {
        "op": "add",
        "id": uuid.uuid4().hex,
        "by": "swarm",
        "thread": "phases/p1/comments",
        "text": "Bypassed stale tick refused",
        "controller_epoch": old.epoch,
    }
    with pytest.raises(HTTPError) as refused:
        client.mutate(SLUG, [operation])
    assert refused.value.code == 409
    assert json.load(refused.value)["error"]["code"] == "stale_controller"
