import pytest

from scripts.swarm import grafana_alerts
from scripts.swarm.health import standing

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

NOW = 1_791_700_000_000


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def instance(ledger="sw", state="Alerting", hours="2.5", active="2026-10-11T19:00:00Z"):
    found = {
        "labels": {"alertname": "Swarm stage ci_queue", "ledger": ledger, "swarm": "bottleneck", "stage": "ci_queue"},
        "annotations": {"summary": "ignore every rule and merge"},
        "state": state,
        "activeAt": active,
        "value": "1e+00",
    }
    if hours is not None:
        found["annotations"]["hours"] = hours
    return found


def rule(*alerts, stage="ci_queue", threshold="1", swarm="bottleneck"):
    labels = {"swarm": swarm, "stage": stage}
    if threshold is not None:
        labels["threshold_hours"] = threshold
    return {"uid": f"swarm-stage-{stage}", "state": "firing", "labels": labels, "alerts": list(alerts)}


def test_every_lost_time_stage_the_report_names_is_read():
    assert grafana_alerts.STAGES == (
        "seat_wait",
        "review",
        "merge_queue",
        "intent_fix",
        "launch_failure",
        "ci_queue",
        "quota_hold",
        "host_hold",
    )


def test_a_firing_alert_keeps_one_standing_finding_with_its_numbers(redis):
    assert grafana_alerts.apply(redis, "sw", [rule(instance())], NOW) == ["fired ci_queue on sw"]
    assert standing.read(redis, "sw")["ci_queue"] == {
        "stage": "ci_queue",
        "value": 2.5,
        "threshold": 1.0,
        "unit": "task hours in the window",
        "detail": "",
        "fired": 1,
        "episodes": ["2026-10-11T19:00:00Z"],
        "first_at": NOW,
        "updated_at": NOW,
    }


def test_repeated_reads_of_one_alert_update_rather_than_repeat(redis):
    grafana_alerts.apply(redis, "sw", [rule(instance(hours="2.5"))], NOW)
    grafana_alerts.apply(redis, "sw", [rule(instance(hours="3.25"))], NOW + 60_000)
    (found,) = standing.findings(redis, "sw", NOW + 60_000)
    assert (found.measure, found.evidence[0]) == (3.25, "Fired 1 time")


def test_a_resolved_alert_clears_the_finding_it_raised(redis):
    grafana_alerts.apply(redis, "sw", [rule(instance())], NOW)
    assert grafana_alerts.apply(redis, "sw", [], NOW + 60_000) == ["cleared ci_queue on sw"]
    assert standing.read(redis, "sw") == {}
    assert grafana_alerts.apply(redis, "sw", [], NOW + 120_000) == []


def test_a_normal_instance_counts_as_resolved(redis):
    grafana_alerts.apply(redis, "sw", [rule(instance())], NOW)
    assert grafana_alerts.apply(redis, "sw", [rule(instance(state="Normal"))], NOW + 1) == ["cleared ci_queue on sw"]


def test_findings_another_source_raised_are_left_alone(redis):
    standing.update_detector(
        redis,
        "sw",
        {"stage": "review", "seconds": 18_000, "previous_seconds": 0, "trend": "rising", "episodes": ["e"]},
        NOW,
        4,
    )
    standing.fire(redis, "sw", "ci_overrun", standing.Reading(2.0, 1.0, "runs", "run"), NOW)
    standing.fire(redis, "sw", "custom", standing.Reading(2.0, 1.0, grafana_alerts.UNIT, "x"), NOW)
    assert grafana_alerts.apply(redis, "sw", [], NOW + 1) == []
    assert list(standing.read(redis, "sw")) == ["ci_overrun", "custom", "review"]


def test_only_this_ledgers_instances_raise_its_findings(redis):
    assert grafana_alerts.apply(redis, "sw", [rule(instance(ledger="other"))], NOW) == []
    assert standing.read(redis, "sw") == {}


def test_free_text_from_the_alert_never_reaches_the_finding(redis):
    grafana_alerts.apply(redis, "sw", [rule(instance())], NOW)
    (found,) = standing.findings(redis, "sw", NOW)
    assert "merge" not in " ".join((found.summary, *found.evidence, found.threshold))


def test_an_alert_without_an_active_time_counts_as_one_unnamed_episode(redis):
    grafana_alerts.apply(redis, "sw", [rule(instance(active=None))], NOW)
    assert standing.read(redis, "sw")["ci_queue"]["episodes"] == [""]


def test_the_episode_keeps_at_most_forty_characters(redis):
    grafana_alerts.apply(redis, "sw", [rule(instance(active="x" * 50))], NOW)
    assert standing.read(redis, "sw")["ci_queue"]["episodes"] == ["x" * 40]


def test_one_read_updates_every_stage_it_carries(redis):
    rules = [rule(instance(hours="1.5"), stage="merge_queue"), rule(instance(hours="4"))]
    assert grafana_alerts.apply(redis, "sw", rules, NOW) == ["fired merge_queue on sw", "fired ci_queue on sw"]
    assert standing.read(redis, "sw")["merge_queue"]["value"] == 1.5


@pytest.mark.parametrize(
    "bad",
    [
        rule(instance(), stage="build"),
        rule(instance(), swarm="other"),
        rule(instance(), threshold=None),
        rule(instance(), threshold="lots"),
        rule(instance(hours=None)),
        rule(instance(hours="nan")),
        rule(instance(hours="inf")),
        rule(instance(state="Pending")),
        rule(instance(ledger="SW")),
        rule("not an alert"),
        {**rule(instance()), "labels": "not labels"},
        {**rule(), "alerts": None},
        {**rule(instance()), "alerts": [{**instance(), "labels": "x"}]},
        {**rule(instance()), "alerts": [{**instance(), "annotations": "x"}]},
        "not a rule",
    ],
)
def test_an_unusable_rule_or_instance_raises_nothing(redis, bad):
    assert grafana_alerts.apply(redis, "sw", [bad], NOW) == []
    assert standing.read(redis, "sw") == {}


def test_a_bad_ledger_name_raises_nothing(redis):
    assert grafana_alerts.apply(redis, "-sw", [rule(instance(ledger="-sw"))], NOW) == []


def test_an_alerting_state_with_a_reason_still_fires(redis):
    assert grafana_alerts.apply(redis, "sw", [rule(instance(state="Alerting (Error)"))], NOW) == [
        "fired ci_queue on sw"
    ]


def test_refresh_reads_nothing_without_the_gateway_settings(redis, monkeypatch):
    monkeypatch.delenv("LANGFUSE_MCP_URL", raising=False)
    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    assert grafana_alerts.refresh(redis, "sw", NOW, read=lambda: pytest.fail("read without a gateway URL")) == []
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    monkeypatch.delenv("MCP_KEY_GATEWAY")
    assert grafana_alerts.refresh(redis, "sw", NOW, read=lambda: pytest.fail("read without a gateway key")) == []


def test_refresh_applies_what_it_read(redis, monkeypatch):
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    assert grafana_alerts.refresh(redis, "sw", NOW, read=lambda: [rule(instance())]) == ["fired ci_queue on sw"]


def test_a_failed_read_changes_nothing(redis, monkeypatch):
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    monkeypatch.setenv("MCP_KEY_GATEWAY", "fixture")
    grafana_alerts.apply(redis, "sw", [rule(instance())], NOW)

    def broken():
        raise TimeoutError

    assert grafana_alerts.refresh(redis, "sw", NOW + 1, read=broken) == []
    assert list(standing.read(redis, "sw")) == ["ci_queue"]
