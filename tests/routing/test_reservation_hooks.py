import json

import pytest

from hooks.context import account_sessions, quota_policy
from scripts import claude_quota_balancer as balancer
from scripts import codex_router
from scripts.codex_quota import CodexQuota
from scripts.routing import reservation
from scripts.routing.master_account import MasterAccount
from scripts.swarm import capacity, quota_handoff, quota_view
from scripts.swarm.store import AgentRecord, RedisStore

ONE = reservation.Reservation("claude", "main", 1)
REASON = "reserved for 1 live master: workers stop at 70% of the week"


def _decide(week_used, ceiling, others=()):
    return quota_policy.decide_reserved(
        ceiling,
        account="main",
        five_used=10.0,
        week_used=week_used,
        five_reset=None,
        week_reset=None,
        others=list(others),
        push=False,
    )


def test_worker_on_the_master_account_hands_off_at_the_ceiling():
    other = quota_policy.Candidate("spare", 10.0, 10.0, 0, 0.0, 6)
    assert _decide(69.9, 70.0, [other]) is None
    decision = _decide(70.0, 70.0, [other])
    assert (decision.action, decision.trigger, decision.target.account) == ("handoff", "week", "spare")
    assert decision.week_used == 70.0
    assert _decide(70.0, None, [other]) is None
    assert _decide(98.5, None, [other]).week_used == 98.5


def test_reserved_candidates_take_no_handoff():
    held = quota_policy.reserved([quota_policy.Candidate("main", 10.0, 70.0, 0, 0.0, 6)], ONE)
    assert held[0].reserved
    assert not quota_policy.reserved([quota_policy.Candidate("main", 10.0, 69.9, 0, 0.0, 6)], ONE)[0].reserved
    assert quota_policy.reserved(held, None) == held
    decision = quota_policy.decide(
        account="tired",
        five_used=10.0,
        week_used=99.0,
        five_reset=None,
        week_reset=None,
        others=held,
        push=False,
    )
    assert decision.action == "stop"


def test_a_master_session_ignores_the_ceiling():
    assert quota_policy._own_ceiling(ONE, "main", master=True) is None
    assert quota_policy._own_ceiling(ONE, "main", master=False) == 70.0
    assert quota_policy._own_ceiling(ONE, "other", master=False) is None
    assert quota_policy._own_ceiling(None, "main", master=False) is None


def _row(name="main", week_left=30.0, harness="claude", **extra):
    return capacity.Account(harness, name, "OPEN", 1, 80.0, week_left, 6, **extra)


def test_capacity_closes_the_master_account_for_workers():
    rows = capacity.reserve([_row(), _row(week_left=30.1), _row("other"), _row(harness="codex")], ONE)
    assert [(row.cap, row.state, row.reserved) for row in rows] == [
        (0, "CLOSED", REASON),
        (6, "OPEN", ""),
        (6, "OPEN", ""),
        (6, "OPEN", ""),
    ]
    assert capacity.reserve([_row(week_left=None)], ONE)[0].cap == 6
    assert capacity.reserve([_row(kind="api")], ONE)[0].cap == 6
    assert capacity.reserve([_row()], None)[0].cap == 6


def test_record_keeps_rows_without_a_reservation_unchanged():
    assert "reserved" not in capacity.record(_row())
    assert capacity.record(_row(reserved=REASON))["reserved"] == REASON
    assert capacity.record(_row(kind="api", reserved=REASON))["reserved"] == REASON
    assert "reserved" not in capacity.record(_row(kind="api"))


def test_handoff_trigger_fires_at_the_ceiling():
    thresholds = quota_handoff.Thresholds()
    assert quota_handoff.trigger(_row(reserved=REASON), thresholds) == "week"
    assert quota_handoff.trigger(_row(), thresholds) == ""
    assert quota_handoff.windows(_row(reserved=REASON, week_left=None), thresholds) == []


def test_quota_view_names_the_reservation():
    row = capacity.record(capacity.reserve([_row()], ONE)[0])
    line = quota_view._account_line(row, [row])
    assert line.endswith(f"routing 30% left, {REASON}  sessions 1")
    assert ", reserved" not in quota_view._account_line(capacity.record(_row()), [row])


def _result(name, week_used):
    return balancer.ProbeResult(
        name, "allowed", "NORMAL", 30.0, balancer.QuotaWindow(used=10.0), balancer.QuotaWindow(used=week_used)
    )


def test_claude_token_source_caps_workers_on_the_master_account():
    source = balancer.ClaudeTokenSource(held=ONE)
    assert source.cap(_result("main", 70.0), 0.0) == 0
    assert source.cap(_result("main", 69.9), 0.0) == 6
    assert source.cap(_result("other", 90.0), 0.0) == 6
    assert balancer.ClaudeTokenSource().cap(_result("main", 70.0), 0.0) == 6


def test_codex_source_caps_workers_on_the_master_account():
    held = reservation.Reservation("codex", "default", 2)
    source = codex_router.CodexAccountSource(held=held)
    week = balancer.QuotaWindow(used=60.0)
    reading = CodexQuota(0.0, "pro", balancer.QuotaWindow(used=10.0), week)
    assert source._cap("default", reading, 0.0) == 0
    assert source._cap("spare", reading, 0.0) == codex_router.account_cap(reading, 0.0)
    assert source._cap("default", None, 0.0) is None
    assert codex_router.CodexAccountSource()._cap("default", reading, 0.0) == codex_router.account_cap(reading, 0.0)


def test_balance_table_names_the_reservation():
    master = MasterAccount("claude", "main")
    marks = balancer.RowMarks("", master, REASON)
    table = balancer.render_table([_result("main", 75.0)], now=0, marks=marks)
    assert table.endswith(f"main: {REASON}")
    assert REASON not in balancer.render_table([_result("main", 75.0)], now=0, marks=balancer.RowMarks("", master))
    assert REASON not in balancer.render_table(
        [_result("main", 75.0)], now=0, marks=balancer.RowMarks("", None, REASON)
    )


def test_a_ceiling_handoff_names_the_ceiling_as_its_limit():
    spare = quota_policy.Candidate("spare", 10.0, 10.0, 0, 0.0, 6)
    assert quota_policy._trigger_text(_decide(70.0, 70.0, [spare])) == "7-day quota 70.0% used (limit 70%)"
    assert _decide(98.5, None, [spare]).week_limit == quota_policy.QUOTA_HANDOFF_WEEK_PCT


def test_a_spent_five_hour_window_at_the_ceiling_leaves_rather_than_waits():
    decision = quota_policy.decide_reserved(
        70.0,
        account="main",
        five_used=99.5,
        week_used=75.0,
        five_reset=1.0,
        week_reset=None,
        others=[],
        push=False,
    )
    assert (decision.action, decision.trigger, decision.week_used) == ("stop", "week", 75.0)


def _windows(monkeypatch, week_used):
    monkeypatch.delenv("AH_ROUTE_API", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.setattr(quota_policy, "_session_windows", lambda session_id: (10.0, week_used, None, None))
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 1)
    monkeypatch.setattr(account_sessions, "session_account", lambda pid: "main")
    calls = []
    monkeypatch.setattr(reservation, "current", lambda harness, environ: calls.append(harness))
    return calls


def test_evaluate_reads_no_reservation_below_the_lowest_ceiling(monkeypatch):
    calls = _windows(monkeypatch, 49.9)
    assert quota_policy.evaluate("s") is None
    assert calls == []


def test_evaluate_reads_the_reservation_from_the_lowest_ceiling(monkeypatch):
    calls = _windows(monkeypatch, 50.0)
    assert quota_policy.evaluate("s") is None
    assert calls == ["claude"]


@pytest.mark.xdist_group("fakeredis")
def test_warn_spares_the_master_lane_on_a_reserved_account():
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    storage.put_agent("sw", AgentRecord("master@x", "master", "m", harness="claude", account="main"))
    storage.put_agent("sw", AgentRecord("worker", "eng", "w", harness="claude", account="main"))
    rows = [capacity.record(_row(reserved=REASON))]
    storage.redis.set(storage.key("sw", "quota-capacity"), json.dumps({"accounts": rows}))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to worker"]


MASTER_ENV = {"AGENTIHOOKS_AGENT_NAME": "master@1"}
WORKER_ENV = {"AGENTIHOOKS_AGENT_NAME": "engineer@1"}


def test_claude_routing_skips_the_master_account_for_workers_only(monkeypatch, tmp_path):
    monkeypatch.setattr(reservation, "current", lambda harness, environ: reservation.Reservation(harness, "MAIN", 1))
    monkeypatch.setattr(
        balancer, "collect_results", lambda *a, **k: ([_result("MAIN", 75.0), _result("SPARE", 30.0)], "live")
    )
    tokens = {"AH_CC_TOKEN_MAIN": "main-secret", "AH_CC_TOKEN_SPARE": "spare-secret"}

    def chosen(environ, sessions):
        decision = balancer.select_credential(
            {**tokens, **environ}, cache_file=tmp_path / "cache.json", sessions=sessions, now=0
        )
        return decision.credential.env_name

    assert chosen(WORKER_ENV, {"MAIN": 0}) == "AH_CC_TOKEN_SPARE"
    assert chosen(MASTER_ENV, {"SPARE": 6}) == "AH_CC_TOKEN_MAIN"


def test_codex_routing_skips_the_master_account_for_workers_only(monkeypatch):
    monkeypatch.setattr(reservation, "current", lambda harness, environ: reservation.Reservation(harness, "default", 1))
    quotas = {"default": CodexQuota(0.0, "team", balancer.QuotaWindow(20.0), balancer.QuotaWindow(80.0))}
    pool = [codex_router.CodexAccount("default")]
    account, _, _ = codex_router.select(pool, quotas, {}, 0.0, environ=MASTER_ENV)
    assert account.name == "default"
    with pytest.raises(codex_router.RoutingError):
        codex_router.select(pool, quotas, {}, 0.0, environ=WORKER_ENV)
