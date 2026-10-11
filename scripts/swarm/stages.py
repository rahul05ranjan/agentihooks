"""Every task second in the window lands in exactly one named stage.

Base phases run claim wait, build, ci run and review; overlays take an instant from its base in a fixed priority,
and an instant of claim wait while host samples hold spawns is that holder's hold.
"""

import json
from dataclasses import dataclass

from scripts.swarm import bottleneck, metrics_outbox
from scripts.swarm.store import RedisStore

KEY = "bottleneck-stages"
TOP = 3
SOURCES = (*bottleneck.SOURCES, "ci_stages", "ledger_events")
READY = ("added", "task open")
DONE = "task done"
JOINED = "joined"
OVERLAYS = ("merge_queue", "intent_fix", "launch_failure", "launch", "claim_wait", "ci_overrun", "ci_queue")
INTENT = ("intent_failed", "intent_passed")
SPOKEN = {"ci_overrun": "in"}
TABLE = metrics_outbox.Table(
    "bottleneck_stages",
    (("stage", "String"), ("label", "String"), ("seconds", "Float64"), ("window_s", "Float64")),
)


@dataclass(frozen=True)
class Span:
    stage: str
    start: float
    end: float
    label: str = ""

    def covers(self, at: float) -> bool:
        return self.start <= at < self.end


@dataclass(frozen=True)
class Timeline:
    start: float
    end: float
    base: tuple
    overlays: tuple


def _ready(task: dict, events: list, before: float) -> float | None:
    own = f"tasks/{task['id']}"
    needed = {f"tasks/{name}" for name in task.get("depends_on") or []}
    marks = [
        event["ts_ms"]
        for event in events
        if event["ts_ms"] <= before
        and (
            (event["target"] == own and event["kind"] in READY) or (event["kind"] == DONE and event["target"] in needed)
        )
    ]
    return max(marks, default=None)


def _lives(task_id: str, rows: dict, end: float) -> list[tuple[Span, Span]]:
    mine = [event for event in rows["agent_events"] if event["task"] == task_id]
    starts = {event["agent"]: event for event in mine if event["kind"] == "spawn"}
    stops = {event["agent"]: event["ts_ms"] for event in mine if event["kind"] == "retire"}
    failed = {event["agent"] for event in mine if event["kind"] == "launch_failed"}
    joins = [event for event in rows["ledger_events"] if event["kind"] == JOINED]
    found = []
    for name, spawn in sorted(starts.items(), key=lambda item: item[1]["ts_ms"]):
        start, stop = spawn["ts_ms"], min(stops.get(name, end), end)
        life = Span("life", start, stop, spawn.get("harness", ""))
        if name in failed:
            found.append((life, Span("launch_failure", start, stop, life.label)))
            continue
        joined = min(
            (event["ts_ms"] for event in joins if event["by"] == name and event["ts_ms"] >= start), default=stop
        )
        found.append((life, Span("launch", start, min(joined, stop), life.label)))
    return found


def _gaps(lives: list[Span], start: float, end: float) -> list[Span]:
    gaps, cursor = [], start
    for life in sorted(lives, key=lambda span: span.start):
        if life.start > cursor:
            gaps.append(Span("claim_wait", cursor, min(life.start, end)))
        cursor = max(cursor, life.end)
    if cursor < end:
        gaps.append(Span("claim_wait", cursor, end))
    return gaps


def _ci(runs: list, stage_rows: list, pushed: float, green: float) -> list[Span]:
    spans = []
    for run in runs:
        created, ended = bottleneck._created(run), run["ts_ms"]
        if ended < pushed or created >= green:
            continue
        mine = [row for row in stage_rows if (row["run_id"], row["attempt"]) == (run["run_id"], run["attempt"])]
        slowest = max(mine, key=lambda row: (row["pickup_s"], row["stage"]), default={})
        queued = (run["queue_s"] + slowest.get("pickup_s", 0.0)) * 1000
        spans.append(Span("ci_queue", created, min(ended, created + queued), slowest.get("runner", "")))
        worst = max(mine, key=lambda row: (_over(row), row["stage"]), default={})
        if worst and _over(worst) > 0:
            spans.append(Span("ci_overrun", max(created, ended - _over(worst) * 1000), ended, worst["stage"]))
    return [Span(span.stage, max(span.start, pushed), min(span.end, green), span.label) for span in spans]


def _over(row: dict) -> float:
    return row["seconds"] - row["budget"] if row["budget"] else 0.0


def _reviews(task_id: str, rows: dict, end: float) -> list[Span]:
    mine = sorted(
        (event for event in rows["delivery_events"] if event["task"] == task_id and event["ts_ms"] <= end),
        key=lambda event: event["ts_ms"],
    )
    verdicts = [event for event in mine if event["kind"] in INTENT]
    spans = [
        Span("intent_fix", event["ts_ms"], next((later["ts_ms"] for later in verdicts[index + 1 :]), end))
        for index, event in enumerate(verdicts)
        if event["kind"] == "intent_failed"
    ]
    queued = [event["ts_ms"] for event in mine if event["kind"] == "merge_queued"]
    return spans + ([Span("merge_queue", queued[-1], end)] if queued else [])


def _claimed(task: dict, rows: dict, now_ms: int, live: bool) -> Timeline:
    task_id = task["id"]
    mine = [event for event in rows["agent_events"] if event["task"] == task_id]
    claimed = min(event["ts_ms"] for event in mine if event["kind"] == "claim")
    merges = [
        event["ts_ms"] for event in rows["delivery_events"] if event["task"] == task_id and event["kind"] == "merge"
    ]
    end = merges[0] if merges else (now_ms if live else max(event["ts_ms"] for event in mine))
    branch = task.get("branch")
    runs = [run for run in rows["ci_runs"] if branch and run["branch"] == branch]
    opened = [
        event["ts_ms"]
        for event in rows["delivery_events"]
        if event["task"] == task_id and event["kind"] == "pull_request_opened"
    ]
    pushed = min([at for at in [*map(bottleneck._created, runs), *opened] if at >= claimed] + [end])
    green = bottleneck._green(runs, pushed, end)
    ready = _ready(task, rows["ledger_events"], claimed)
    start = claimed if ready is None else ready
    lives = _lives(task_id, rows, end)
    base = (
        Span("claim_wait", start, claimed),
        Span("build", claimed, pushed),
        Span("ci_run", pushed, green),
        Span("review", green, end),
    )
    overlays = (
        *_reviews(task_id, rows, end),
        *(launch for _, launch in lives),
        *_gaps([life for life, _ in lives], claimed, end),
        *_ci(runs, rows["ci_stages"], pushed, green),
    )
    return Timeline(start, end, base, overlays)


def _waiting(task: dict, rows: dict, done: set, now_ms: int) -> Timeline | None:
    if task.get("state") != "open" or not set(task.get("depends_on") or []) <= done:
        return None
    ready = _ready(task, rows["ledger_events"], now_ms)
    if ready is None:
        return None
    return Timeline(ready, now_ms, (Span("claim_wait", ready, now_ms),), ())


def timelines(rows: dict, tasks: list, now_ms: int) -> list[Timeline]:
    claimed = {event["task"] for event in rows["agent_events"] if event["kind"] == "claim"}
    by_id = {task["id"]: task for task in tasks}
    done = {task["id"] for task in tasks if task.get("state") == "done"}
    found = [
        _claimed(
            by_id.get(task_id, {"id": task_id}), rows, now_ms, by_id.get(task_id, {}).get("state") in bottleneck.OPEN
        )
        for task_id in sorted(claimed)
    ]
    waiting = [_waiting(task, rows, done, now_ms) for task in tasks if task["id"] not in claimed]
    return found + [timeline for timeline in waiting if timeline is not None]


def holds(samples: list, now_ms: int) -> list[Span]:
    held = [
        Span(f"{share}_hold", start, end)
        for share, start, end, count in bottleneck.held_spans(samples, now_ms)
        if count > 0 and end > start
    ]
    merged = []
    for span in held:
        if merged and merged[-1].stage == span.stage and merged[-1].end >= span.start:
            merged[-1] = Span(span.stage, merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    return merged


def _stage_at(timeline: Timeline, held: list[Span], at: float) -> tuple[str, str]:
    ranked = sorted(timeline.overlays, key=lambda span: OVERLAYS.index(span.stage))
    span = next((span for span in (*ranked, *timeline.base) if span.covers(at)), Span("claim_wait", at, at))
    if span.stage == "claim_wait":
        hold = next((hold for hold in held if hold.covers(at)), None)
        if hold is not None:
            return hold.stage, ""
    return span.stage, span.label


def attribute(timeline: Timeline, held: list[Span], window: bottleneck.Window) -> dict:
    low, high = max(timeline.start, window.start), min(timeline.end, window.end)
    spans = [*timeline.base, *timeline.overlays, *held]
    points = sorted({low, high, *(edge for span in spans for edge in (span.start, span.end) if low < edge < high)})
    seconds = {}
    for start, end in zip(points, points[1:]):
        key = _stage_at(timeline, held, (start + end) / 2)
        seconds[key] = seconds.get(key, 0.0) + (end - start) / 1000
    return seconds


def report(rows: dict, tasks: list, now_ms: int, window_ms: int = bottleneck.WINDOW_MS) -> dict:
    window = bottleneck.Window(now_ms - window_ms, now_ms)
    held = holds(rows["host_samples"], now_ms)
    seconds = {}
    for timeline in timelines(rows, tasks, now_ms):
        for key, value in attribute(timeline, held, window).items():
            seconds[key] = seconds.get(key, 0.0) + value
    found = [
        {"stage": stage, "label": label, "seconds": round(value, 1)}
        for (stage, label), value in seconds.items()
        if round(value, 1) > 0
    ]
    found.sort(key=lambda row: (-row["seconds"], row["stage"], row["label"]))
    return {
        "at": now_ms,
        "window_hours": window_ms / 3_600_000,
        "stages": found,
        "total": round(sum(seconds.values()), 1),
    }


def _named(row: dict) -> str:
    name = row["stage"].replace("_", " ")
    return f"{name} {SPOKEN.get(row['stage'], 'on')} {row['label']}" if row["label"] else name


def line(found: dict, now_ms: int) -> str:
    if not found:
        return "top stages not measured yet: the metrics sink is off or no tick has run"
    hours = f"{found['window_hours']:g}"
    if not found["total"]:
        return f"top stages none: no task time in the last {hours} hours"
    top = ", ".join(f"{_named(row)} {row['seconds'] / 3600:.1f} task hours" for row in found["stages"][:TOP])
    age = (now_ms - found["at"]) // 60_000
    return (
        f"top stages in the last {hours} hours: {top}; {found['total'] / 3600:.1f} task hours in all,"
        f" measured {age} minutes ago"
    )


def read_rows(box: metrics_outbox.Outbox, slug: str, now_ms: int) -> dict:
    return {name: [found for found in box.recent(name, now_ms) if found["ledger"] == slug] for name in SOURCES}


def stage_rows(slug: str, found: dict) -> list[dict]:
    return [
        {
            "event_id": f"stage:{slug}:{found['at']}:{row['stage']}:{row['label']}",
            "ledger": slug,
            "ts_ms": found["at"],
            **dict.fromkeys(("plan", "phase", "slice", "task"), ""),
            "stage": row["stage"],
            "label": row["label"],
            "seconds": row["seconds"],
            "window_s": found["window_hours"] * 3600,
        }
        for row in found["stages"]
    ]


def record(box: metrics_outbox.Outbox, store: RedisStore, slug: str, now_ms: int, tasks: list) -> dict:
    found = report(read_rows(box, slug, now_ms), tasks, now_ms)
    box.append(TABLE, stage_rows(slug, found))
    store.redis.set(store.key(slug, KEY), json.dumps(found))
    return found


def read(store: RedisStore, slug: str) -> dict:
    value = store.redis.get(store.key(slug, KEY))
    return json.loads(value) if value else {}
