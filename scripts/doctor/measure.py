"""The number doctor measure prints for one finding id or one finding class, refusing any it cannot stand behind."""

from dataclasses import dataclass

from scripts.doctor import traces
from scripts.swarm.store import SwarmError

LAG_CLASSES = ("telemetry-stale", "exporter-backlog")


@dataclass(frozen=True)
class Read:
    count: int
    unknown: int
    maximum: int

    def line(self, name):
        return f"{name} count {self.count} unknown {self.unknown} max {self.maximum}"


def class_of(kind):
    return kind.replace(" ", "-")


def _agent(evidence):
    return next((e[len("agent ") :] for e in evidence if e.startswith("agent ")), "")


def _class_hint(finding_id):
    return f"measure the class {finding_id.split('/', 1)[0]} to read every live target"


def exact(finding_id, found, failed, seen, live):
    """A current finding's number; 0 only for a seen finding that is gone while the agent it names is live."""
    match = next((f for f in found if f.id == finding_id), None)
    if match:
        return match.measure
    if failed:
        raise SwarmError(f"{finding_id} is unknown while {len(failed)} detector{'s' * (len(failed) != 1)} failed")
    record = seen.get(finding_id)
    if record is None:
        raise SwarmError(f"no finding {finding_id} has been seen; {_class_hint(finding_id)}")
    agent = _agent(record["evidence"])
    if agent and agent not in live:
        raise SwarmError(f"{finding_id} names agent {agent}, which is no longer live; {_class_hint(finding_id)}")
    return 0


def _measures(name, found):
    return [f.measure for f in found if class_of(f.kind) == name]


def of_class(name, found, failed, seen_classes):
    """Every current finding of a class seen before; unknown counts the detectors that failed."""
    measures = _measures(name, found)
    if not measures and name not in seen_classes:
        raise SwarmError(f"no finding of the class {name} has been seen")
    return Read(len(measures), len(failed), max(measures, default=0))


def lag(local, now_ms):
    return None if local is None else traces._lag({"local": local}, now_ms)


def of_lag_class(name, found, lags):
    """Every active binding: unknown when its exporter progress is unreadable, the maximum over its unaccepted
    seconds and the class's current findings."""
    known = [seconds for seconds in lags if seconds is not None]
    return Read(len(lags), len(lags) - len(known), max([*known, *_measures(name, found)], default=0))
