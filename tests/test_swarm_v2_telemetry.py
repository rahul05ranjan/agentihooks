import json
from pathlib import Path

import pytest

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import telemetry
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.runtime import observe
from scripts.swarm_v2.runtime.operations import Observation, OperationRequest, Phase
from tests import sv2_obs01_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-OBS-01"
SLUG = "fixture"
NOW = 5000
STATUS_FIELDS = ("state", "events", "unexported", "export_failures", "last_error", "failed_at_ms", "exported_at_ms")
PASS = "password"
CLIENT = "client_secret"
AWS = "AKIA0123456789ABCDEF"


class Transport:
    backend = "kubernetes"

    def __init__(self):
        self.effects = {}

    def observe_operation(self, operation):
        return self.effects.get(operation.operation_id, Observation(Phase.ABSENT))

    def apply_operation(self, operation, payload):
        result = Observation(
            Phase.APPLIED,
            operation.execution_id,
            operation.generation,
            operation.backend,
            operation.payload_digest,
            {"uid": "confirmed"},
        )
        self.effects[operation.operation_id] = result
        return result


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
    monkeypatch.setattr(lease, "now_ms", lambda store: NOW)
    return store


def _agent(store):
    return AgentRecord(
        store.next_name(SLUG, "eng"),
        "eng",
        "task-one",
        seat=f"eng-1@{SLUG}",
        runtime_backend="kubernetes",
        runtime_target={"pod_namespace": "swarm", "pod_name": "swarm-pending"},
    )


def _status(**values):
    return {**dict.fromkeys(STATUS_FIELDS, 0), "state": "disabled", "last_error": "", **values}


@pytest.mark.parametrize(
    ("text", "clean"),
    (
        ("Authorization failed: bearer abcdefgh", "Authorization failed: [redacted]"),
        ("refused sk-ant-0123456789 at gateway", "refused [redacted] at gateway"),
        ("pushed with ghp_abcdefghij1234", "pushed with [redacted]"),
        ("signed by xoxb-0123456789-abc", "signed by [redacted]"),
        (f"key {AWS} here", "key [redacted] here"),
        (f"{PASS}=hunter22 user=bob", f"{PASS}=[redacted] user=bob"),
        ("{'api_key': 'abc123', 'mode': 'x'}", "{'api_key': '[redacted]', 'mode': 'x'}"),
        (f'{CLIENT} = "s3cr3t" next', f'{CLIENT} = "[redacted]" next'),
        ("x-github-token: abc.def", "x-github-token: [redacted]"),
        ("passwd: hunter2", "passwd: [redacted]"),
        ("TOKEN=abc", "TOKEN=[redacted]"),
        ("aws credentials=AbC/12", "aws credentials=[redacted]"),
        ("sk-short and a plain sentence", "sk-short and a plain sentence"),
    ),
)
def test_each_secret_shape_is_redacted_and_plain_text_is_kept(text, clean):
    assert telemetry.sanitize_text(text) == clean


def test_an_environment_dump_is_withheld_whole_and_two_pairs_are_kept():
    dump = "environ({'HOME': '/root', 'PATH': '/bin', 'SHELL': '/bin/sh'})"

    assert telemetry.sanitize_text(dump) == "[environment withheld]"
    assert telemetry.sanitize_text("HOME=/root PATH=/bin") == "HOME=/root PATH=/bin"


def test_whitespace_collapses_and_long_text_is_cut_to_the_limit():
    assert telemetry.sanitize_text("a\n  b\tc") == "a b c"
    assert telemetry.sanitize_text("x" * 240) == "x" * 240
    assert telemetry.sanitize_text("x" * 241) == "x" * 239 + "…"


def test_an_error_keeps_its_type_and_a_redacted_message():
    assert telemetry.sanitize_error(ValueError("token=abc")) == "ValueError: token=[redacted]"


@pytest.mark.parametrize(
    ("seconds", "le"), ((0.0, "0.01"), (0.01, "0.01"), (0.011, "0.05"), (1.0, "1"), (30.0, "30"), (30.5, "+Inf"))
)
def test_a_duration_lands_in_the_first_bucket_that_holds_it(seconds, le):
    assert telemetry.bucket(seconds) == le


def test_a_record_keeps_allowlisted_fields_and_counts_only_fixed_labels(store):
    recorder = telemetry.Telemetry(store, SLUG)

    event = recorder.record(
        "admission",
        "accepted",
        0.2,
        task="task-one",
        seat="eng-1@fixture",
        execution_id="exec-1",
        expected_generation=0,
        observed_generation=1,
        controller_epoch=True,
        environment={"HOME": "/root"},
        payload="body",
        reason="",
        mode=None,
    )

    assert event == {
        "transition": "admission",
        "result": "accepted",
        "duration_ms": 200,
        "task": "task-one",
        "seat": "eng-1@fixture",
        "execution_id": "exec-1",
        "expected_generation": 0,
        "observed_generation": 1,
        "controller_epoch": "True",
        "at_ms": NOW,
        "seq": 1,
    }
    assert recorder.events() == [event]
    assert recorder.execution_transition_latency_seconds()["admission"]["accepted"] == {
        "buckets": {"0.01": 0, "0.05": 0, "0.1": 0, "0.5": 1, "1": 1, "5": 1, "30": 1, "+Inf": 1},
        "count": 1,
        "sum": 0.2,
    }
    assert sorted(store.redis.hkeys(store.key(SLUG, telemetry.LATENCY))) == [
        "admission|accepted|0.5",
        "admission|accepted|sum_ms",
    ]
    assert telemetry.observability(store, SLUG) == _status(events=1)


def test_every_transition_and_result_has_a_zeroed_histogram_row(store):
    histogram = telemetry.Telemetry(store, SLUG).execution_transition_latency_seconds()

    assert list(histogram) == ["admission", "command", "heartbeat", "outcome", "fence", "recovery"]
    assert all(list(rows) == ["accepted", "refused", "failed"] for rows in histogram.values())
    assert histogram["fence"]["failed"] == {"buckets": dict.fromkeys(telemetry.BUCKETS, 0), "count": 0, "sum": 0.0}


def test_a_negative_duration_counts_as_zero(store):
    recorder = telemetry.Telemetry(store, SLUG)

    assert recorder.record("fence", "accepted", -1.0)["duration_ms"] == 0
    assert recorder.execution_transition_latency_seconds()["fence"]["accepted"]["buckets"]["0.01"] == 1


@pytest.mark.parametrize(("transition", "result"), (("launch", "accepted"), ("admission", "ignored")))
def test_an_unknown_transition_or_result_is_refused(store, transition, result):
    with pytest.raises(ValueError) as refused:
        telemetry.Telemetry(store, SLUG).record(transition, result, 0.0)

    assert str(refused.value) == "unknown transition or result"
    assert store.redis.get(store.key(SLUG, telemetry.SEQUENCE)) is None


def test_an_unaudited_transition_is_counted_without_an_event_or_export(store):
    exporter = cases.Exporter()
    recorder = telemetry.Telemetry(store, SLUG, exporter)

    event = recorder.record("heartbeat", "accepted", 0.0, False, execution_id="exec-1")

    assert event == {
        "transition": "heartbeat",
        "result": "accepted",
        "duration_ms": 0,
        "execution_id": "exec-1",
        "at_ms": NOW,
    }
    assert recorder.events() == []
    assert exporter.received == []
    assert store.redis.get(store.key(SLUG, telemetry.SEQUENCE)) is None
    assert recorder.execution_transition_latency_seconds()["heartbeat"]["accepted"]["count"] == 1


def test_an_exporter_outage_degrades_without_raising_and_replays_each_missed_event_once(store):
    exporter = cases.Exporter()
    recorder = telemetry.Telemetry(store, SLUG, exporter)
    recorder.record("admission", "accepted", 0.0, seat="eng-1@fixture")
    exporter.down = True

    assert recorder.record("command", "accepted", 0.0, execution_id="exec-1")["seq"] == 2
    recorder.record("command", "refused", 0.0, execution_id="exec-1")

    error = "ConnectionError: collector unreachable with token=[redacted]"
    assert recorder.last_error == error
    assert telemetry.observability(store, SLUG) == _status(
        state="degraded",
        events=3,
        unexported=2,
        export_failures=2,
        last_error=error,
        failed_at_ms=NOW,
        exported_at_ms=NOW,
    )
    exporter.down = False
    recorder.record("heartbeat", "accepted", 0.0, execution_id="exec-1")

    assert [event["seq"] for event in exporter.received] == [1, 2, 3, 4]
    assert exporter.received == recorder.events()
    assert telemetry.observability(store, SLUG) == _status(
        state="ok", events=4, export_failures=2, last_error=error, failed_at_ms=NOW, exported_at_ms=NOW
    )


def test_a_trimmed_audit_exports_what_it_still_holds_without_repeats(store, monkeypatch):
    monkeypatch.setattr(telemetry, "AUDIT_LIMIT", 3)
    exporter = cases.Exporter()
    recorder = telemetry.Telemetry(store, SLUG, exporter)
    for _ in range(2):
        recorder.record("fence", "accepted", 0.0)
    exporter.down = True
    for _ in range(3):
        recorder.record("fence", "accepted", 0.0)
    exporter.down = False
    recorder.record("fence", "accepted", 0.0)
    recorder.record("fence", "accepted", 0.0)

    assert [event["seq"] for event in recorder.events()] == [5, 6, 7]
    assert [event["seq"] for event in exporter.received] == [1, 2, 4, 5, 6, 7]
    assert telemetry.observability(store, SLUG)["unexported"] == 0


def test_a_failed_audit_write_is_kept_as_the_last_error_and_never_raised(store, monkeypatch):
    def down(name):
        raise ConnectionError(f"redis down {PASS}=pw1")

    monkeypatch.setattr(store.redis, "incr", down)
    recorder = telemetry.Telemetry(store, SLUG, cases.Exporter())

    assert recorder.record("admission", "accepted", 0.0) is None
    assert recorder.last_error == f"ConnectionError: redis down {PASS}=[redacted]"


def test_a_degraded_state_that_cannot_be_stored_is_kept_in_memory(store, monkeypatch):
    exporter = cases.Exporter()
    exporter.down = True

    def down(*args, **kwargs):
        raise ValueError("hset down")

    monkeypatch.setattr(store.redis, "hset", down)
    recorder = telemetry.Telemetry(store, SLUG, exporter)

    assert recorder.record("admission", "accepted", 0.0)["seq"] == 1
    assert recorder.last_error == "ValueError: hset down"


def test_timed_records_accepted_refused_and_failed_transitions_and_reraises(store):
    recorder = telemetry.Telemetry(store, SLUG, clock=cases.Ticks(0.25))
    with recorder.timed("command", execution_id="exec-1") as seen:
        seen["operation_id"] = "op-1"
    with pytest.raises(SwarmError) as refused, recorder.timed("admission", seat="eng-1@fixture"):
        raise SwarmError("lease lost token=abc")
    with pytest.raises(RuntimeError) as failed, recorder.timed("heartbeat", execution_id="exec-1"):
        raise RuntimeError("boom")
    with recorder.timed("heartbeat", execution_id="exec-1") as seen:
        seen["audit"] = False

    assert (str(refused.value), str(failed.value)) == ("lease lost token=abc", "boom")
    assert [(e["transition"], e["result"], e.get("error", ""), e["duration_ms"]) for e in recorder.events()] == [
        ("command", "accepted", "", 250),
        ("admission", "refused", "SwarmError: lease lost token=[redacted]", 250),
        ("heartbeat", "failed", "RuntimeError: boom", 250),
    ]
    assert recorder.events()[0]["operation_id"] == "op-1"
    assert recorder.execution_transition_latency_seconds()["heartbeat"]["accepted"]["count"] == 1


def test_events_filter_on_every_named_field(store):
    recorder = telemetry.Telemetry(store, SLUG)
    recorder.record("command", "accepted", 0.0, execution_id="exec-1")
    recorder.record("command", "refused", 0.0, execution_id="exec-1")
    recorder.record("command", "accepted", 0.0, execution_id="exec-2")

    assert [e["seq"] for e in recorder.events(execution_id="exec-1", result="accepted")] == [1]


def test_history_joins_a_seats_commands_through_its_execution_ids():
    events = [
        {"seq": 1, "transition": "admission", "seat": "eng-1", "execution_id": "a"},
        {"seq": 2, "transition": "command", "execution_id": "a"},
        {"seq": 3, "transition": "admission", "seat": "eng-2", "execution_id": "b"},
        {"seq": 4, "transition": "command", "execution_id": "b"},
        {"seq": 5, "transition": "recovery", "seat": "eng-1", "execution_id": "a", "replacement": "c"},
        {"seq": 6, "transition": "command", "execution_id": "c"},
        {"seq": 7, "transition": "admission", "seat": "eng-1", "result": "refused"},
    ]

    assert [event["seq"] for event in telemetry.history(events, "eng-1")] == [1, 2, 5, 6, 7]


def test_stdout_prints_one_sorted_json_line(capsys):
    telemetry.stdout({"b": 1, "a": "x"})

    assert capsys.readouterr().out == 'transition: {"a": "x", "b": 1}\n'


@pytest.mark.parametrize(
    ("environ", "expected"),
    (
        ({"AGENTIHOOKS_TRANSITION_EXPORT": "stdout"}, telemetry.stdout),
        ({"AGENTIHOOKS_TRANSITION_EXPORT": "otlp"}, None),
        ({}, None),
    ),
)
def test_only_the_stdout_setting_turns_the_exporter_on(environ, expected):
    assert telemetry.configured_exporter(environ) is expected


def test_observability_of_an_untouched_swarm_reads_disabled(store):
    assert telemetry.observability(store, SLUG) == _status()


def test_the_status_report_carries_observability(store, monkeypatch):
    from scripts.swarm import status

    monkeypatch.setattr(status, "page_quota", lambda: {})
    telemetry.Telemetry(store, SLUG).record("admission", "accepted", 0.0)

    assert status.status_report(store, SLUG, {"tasks": []})["observability"] == _status(events=1)


def test_the_controller_records_each_admission_and_command_with_both_generations(store):
    controller = Controller(store, SLUG, [Transport()], lambda: True)
    controller.telemetry = telemetry.Telemetry(store, SLUG, clock=lambda: 0.0)

    with pytest.raises(SwarmError):
        controller.admit(_agent(store))
    assert controller.acquire()
    attempt = controller.admit(_agent(store))
    operation = controller.execute(
        OperationRequest(attempt.execution_id, attempt.generation, "spawn", {"prompt": "fixture"}, "create")
    )

    epoch = controller.held.epoch
    assert controller.telemetry.events() == [
        {
            "transition": "admission",
            "result": "refused",
            "duration_ms": 0,
            "task": "task-one",
            "seat": f"eng-1@{SLUG}",
            "expected_generation": 1,
            "error": "SwarmError: the controller lease is absent",
            "at_ms": NOW,
            "seq": 1,
        },
        {
            "transition": "admission",
            "result": "accepted",
            "duration_ms": 0,
            "task": "task-one",
            "seat": f"eng-1@{SLUG}",
            "expected_generation": 1,
            "controller_epoch": epoch,
            "execution_id": attempt.execution_id,
            "observed_generation": attempt.generation,
            "at_ms": NOW,
            "seq": 2,
        },
        {
            "transition": "command",
            "result": "accepted",
            "duration_ms": 0,
            "execution_id": attempt.execution_id,
            "action": "spawn",
            "expected_generation": attempt.generation,
            "controller_epoch": epoch,
            "operation_id": operation.operation_id,
            "observed_generation": operation.generation,
            "reason": "applied",
            "at_ms": NOW,
            "seq": 3,
        },
    ]


def test_a_successor_admission_expects_the_next_generation_and_names_its_predecessor(store):
    controller = Controller(store, SLUG, [Transport()], lambda: True)
    assert controller.acquire()
    first = controller.admit(_agent(store))

    second = controller.admit(_agent(store), first.execution_id)

    event = controller.telemetry.events(execution_id=second.execution_id)[0]
    assert (event["previous_execution_id"], event["expected_generation"], event["observed_generation"]) == (
        first.execution_id,
        2,
        second.generation,
    )


def test_a_heartbeat_is_counted_every_time_and_audited_only_when_its_state_changes(store):
    controller = Controller(store, SLUG, [Transport()], lambda: True)
    assert controller.acquire()
    attempt = controller.admit(_agent(store))
    observer = observe.Observer(store, "kubernetes", observe.Thresholds())
    beat = observe.Signal(
        observe.Source.HEARTBEAT, observe.Reading.OK, NOW / 1000, attempt.execution_id, attempt.generation, "working"
    )

    assert controller.observe(observer, attempt, [beat], NOW / 1000) is None
    assert controller.observe(observer, attempt, [beat], NOW / 1000) is None

    beats = controller.telemetry.events(transition="heartbeat")
    assert [(e["execution_id"], e["expected_generation"], e["observed_generation"]) for e in beats] == [
        (attempt.execution_id, attempt.generation, attempt.generation)
    ]
    assert beats[0]["reason"] == observer.get(SLUG, attempt.execution_id).state.value
    assert controller.telemetry.execution_transition_latency_seconds()["heartbeat"]["accepted"]["count"] == 2


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
