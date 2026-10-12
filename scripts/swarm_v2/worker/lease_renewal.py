"""A Pod worker holds its task lease only while it renews it: the controller admits with a short lease and refuses
every scoped write once it lapses. The launch grant that authenticates each renewal expires too and is renewed here."""

import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path

from scripts.swarm_v2 import broadcast_bridge
from scripts.swarm_v2.broadcast_bridge import AUTHORIZATION, JSON_HEADERS

ADMISSION = "admission.json"
SCHEMA_VERSION = "2.1"
HEARTBEAT = "/v2/executions/{}/heartbeat"
CREDENTIAL = "/v2/executions/{}/credential"
INTERVAL_SECONDS = 15.0
RETRY_SECONDS = 2.0
CREDENTIAL_SECONDS = 120.0
TIMEOUT_SECONDS = 5
UNREACHABLE = 503
REFUSED = (401, 403, 409)
AUTHORITY = ("execution_id", "task_id", "task_generation", "controller_epoch", "owner_identity")
EXPECTED = {"heartbeat": ("lease_deadline_ms", int), "credential": ("credential", str)}

Send = Callable[[str, str, str, dict], tuple[int, object]]


def send(method: str, url: str, grant: str, body: dict) -> tuple[int, object]:
    headers = {**JSON_HEADERS, AUTHORIZATION: f"Bearer {grant}"}
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:
            status, text = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (OSError, HTTPException):
        return UNREACHABLE, {}
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {}


def _timestamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class Renewer:
    def __init__(self, attempt: Path, send: Send, clock: Callable[[], float], wall: Callable[[], float]) -> None:
        admission = json.loads((attempt / ADMISSION).read_text())
        launch = json.loads((attempt / "launch.json").read_text())
        self.attempt, self.send, self.clock, self.wall = attempt, send, clock, wall
        self.url = launch["control_url"].rstrip("/")
        self.grant = broadcast_bridge.grant_path(attempt).read_text().strip()
        self.authority = {key: admission[key] for key in AUTHORITY}
        now = clock()
        self.deadline = self._until(admission["lease_deadline_ms"])
        self.due = now
        self.credential_due = now + CREDENTIAL_SECONDS
        self.sequence = 0
        self.lost: str | None = None

    def tick(self) -> None:
        now = self.clock()
        if self.lost is not None or now < self.due:
            return
        if now >= self.deadline:
            self._lose("lease_expired")
            return
        if now >= self.credential_due and not self._credential(now):
            return
        answer = self._call("heartbeat", "PUT", HEARTBEAT, self._body())
        if answer is None:
            return
        if (answer.get("execution_id"), answer.get("task_generation")) != (
            self.authority["execution_id"],
            self.authority["task_generation"],
        ):
            self._lose("generation_changed")
            return
        self.deadline = self._until(answer["lease_deadline_ms"])
        self.due = now + INTERVAL_SECONDS
        self._log("renewed", renewal_sequence=self.sequence, lease_deadline_ms=answer["lease_deadline_ms"])

    def _credential(self, now: float) -> bool:
        answer = self._call("credential", "POST", CREDENTIAL, {})
        if answer is None:
            return False
        self.grant = answer["credential"]
        broadcast_bridge.store_grant(broadcast_bridge.grant_path(self.attempt), self.grant)
        self.credential_due = now + CREDENTIAL_SECONDS
        self._log("credential_renewed", expires_at=answer.get("expires_at"))
        return True

    def _call(self, call: str, method: str, path: str, body: dict) -> dict | None:
        url = self.url + path.format(self.authority["execution_id"])
        status, answer = self.send(method, url, self.grant, body)
        if status in REFUSED:
            self._lose(f"{call}_refused_{status}")
            return None
        key, kind = EXPECTED[call]
        if status != 200 or not isinstance(answer, dict) or type(answer.get(key)) is not kind or not answer[key]:
            self.due = self.clock() + RETRY_SECONDS
            self._log("renewal_failed", call=call, status=status, retry_seconds=RETRY_SECONDS)
            return None
        return answer

    def _body(self) -> dict:
        self.sequence = max(self.sequence + 1, int(self.wall() * 1000))
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": f"heartbeat-{uuid.uuid4().hex}",
            "authority": dict(self.authority),
            "renewal_sequence": self.sequence,
            "state": "working",
            "observed_at": _timestamp(self.wall()),
        }

    def _until(self, deadline_ms: int) -> float:
        return self.clock() + deadline_ms / 1000 - self.wall()

    def _lose(self, reason: str) -> None:
        self.lost = reason
        self._log("lost", reason=reason)

    def _log(self, event: str, **fields: object) -> None:
        identity = {
            "execution_id": self.authority["execution_id"],
            "task_generation": self.authority["task_generation"],
        }
        print(json.dumps({"lease": event, **fields, **identity}), flush=True)


def load(
    attempt: Path, send: Send = send, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time
) -> Renewer | None:
    if not (attempt / ADMISSION).exists():
        return None
    return Renewer(attempt, send, clock, wall)
