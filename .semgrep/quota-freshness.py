from scripts import claude_quota_balancer as balancer
from scripts import quota_freshness, session_bands
from scripts.claude_quota_balancer import account_cap
from scripts.swarm_v2.quota import UNKNOWN


def claude_cap_without_freshness(result, now):
    # ruleid: quota-decision-without-freshness
    return balancer.account_cap(result, now)


def imported_cap_without_freshness(result, now):
    # ruleid: quota-decision-without-freshness
    return account_cap(result, now)


def band_cap_without_freshness(five, week):
    # ruleid: quota-decision-without-freshness
    return session_bands.cap(five, week)


def week_cap_without_freshness(week):
    # ruleid: quota-decision-without-freshness
    return session_bands.week_cap(week)


def drain_without_freshness(account, limits):
    left = account["week_left"]
    # ruleid: quota-decision-without-freshness
    return left is not None and left <= limits.drain_left


def cap_after_the_age_check(result, at, now):
    # ok: quota-decision-without-freshness
    return balancer.account_cap(result, now) if session_bands.fresh(at, now) else None


def drain_after_the_freshness_check(account, limits, now):
    if quota_freshness.stale(account.get("observed_at"), (), now):
        return False
    # ok: quota-decision-without-freshness
    return account["week_left"] <= limits.drain_left


def fleet_cap_on_a_graded_reading(reading):
    if reading.state == UNKNOWN:
        return 0
    # ok: quota-decision-without-freshness
    return session_bands.cap(reading.five_left, reading.week_left)
