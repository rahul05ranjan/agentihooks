from hooks.context import quota_policy
from scripts import claude_quota_balancer as balancer
from scripts import codex_router
from scripts.codex_quota import CodexQuota
from scripts.routing import reservation
from scripts.routing.master_account import MasterAccount
from scripts.swarm import capacity, quota_handoff, quota_view

ONE = reservation.Reservation("claude", "main", 1)
REASON = "reserved for 1 live master: workers stop at 70% of the week"


def _decide(week_used, ceiling, others=()):
    return quota_policy.decide(
        account="main",
        five_used=10.0,
        week_used=week_used,
        five_reset=None,
        week_reset=None,
        others=list(others),
        push=False,
        ceiling=ceiling,
    )


def test_worker_on_the_master_account_hands_off_at_the_ceiling():
    other = quota_policy.Candidate("spare", 10.0, 10.0, 0, 0.0, 6)
    assert _decide(69.9, 70.0, [other]) is None
    decision = _decide(70.0, 70.0, [other])
    assert (decision.action, decision.trigger, decision.target.account) == ("handoff", "week", "spare")
    assert _decide(70.0, None, [other]) is None


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
    row = capacity.record(_row(state="CLOSED", reserved=REASON))
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
