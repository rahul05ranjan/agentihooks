import json
import socket
import time
import uuid
from dataclasses import dataclass

from hooks._redis import redis_key
from scripts.scheduler.slots import Slot, restore


@dataclass(frozen=True)
class Signal:
    scheduler: "Scheduler"
    entry_id: str
    name: str

    def connect(self, slot: Slot) -> str:
        connection_id = uuid.uuid4().hex
        self.scheduler.update(
            self.entry_id, lambda entry: entry["slots"].append({"id": connection_id, **slot.record()})
        )
        return connection_id

    def disconnect(self, connection_id: str) -> None:
        def change(entry):
            entry["slots"] = [slot for slot in entry["slots"] if slot["id"] != connection_id]

        self.scheduler.update(self.entry_id, change)


@dataclass(frozen=True)
class Entry:
    scheduler: "Scheduler"
    id: str

    @property
    def record(self) -> dict:
        raw = self.scheduler.redis.hget(self.scheduler.key("entries"), self.id)
        if raw is None:
            raise KeyError(self.id)
        return json.loads(raw)

    @property
    def state(self) -> str:
        return self.record["state"]

    @property
    def timeout(self) -> Signal:
        return Signal(self.scheduler, self.id, "timeout")

    def cancel(self) -> None:
        def change(entry):
            entry["state"] = "cancelled"

        self.scheduler.update(self.id, change)


class Scheduler:
    def __init__(self, redis, *, machine: str | None = None):
        self.redis = redis
        self.machine = machine or socket.gethostname()

    def key(self, suffix: str) -> str:
        return redis_key("scheduler", f"{self.machine}:{suffix}")

    def get(self, entry_id: str) -> Entry:
        entry = Entry(self, entry_id)
        entry.record
        return entry

    def update(self, entry_id: str, change) -> None:
        from redis.exceptions import WatchError

        key = self.key("entries")
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.hget(key, entry_id)
                    if raw is None:
                        raise KeyError(entry_id)
                    entry = json.loads(raw)
                    change(entry)
                    pipe.multi()
                    pipe.hset(key, entry_id, json.dumps(entry))
                    if entry["state"] == "cancelled":
                        pipe.zrem(self.key("due"), entry_id)
                    pipe.execute()
                    return
                except WatchError:
                    continue

    def timer(
        self, *, owner: str, payload: str, delay: float | None = None, at: float | None = None, now: float | None = None
    ) -> Entry:
        now = time.time() if now is None else now
        if (delay is None) == (at is None):
            raise ValueError("a timer needs either delay or at")
        if delay is not None and delay < 0:
            raise ValueError("delay must not be negative")
        if not owner or not payload.strip():
            raise ValueError("a schedule needs an owner and payload")
        entry_id = uuid.uuid4().hex
        due = at if at is not None else now + delay
        entry = {
            "id": entry_id,
            "kind": "timer",
            "owner": owner,
            "payload": payload,
            "due": due,
            "started_at": now,
            "state": "active",
            "signal": "timeout",
            "slots": [],
        }
        with self.redis.pipeline() as pipe:
            pipe.hset(self.key("entries"), entry_id, json.dumps(entry))
            pipe.zadd(self.key("due"), {entry_id: due})
            pipe.execute()
        return Entry(self, entry_id)

    def _claim(self, entry_id: str, now: float) -> dict | None:
        from redis.exceptions import WatchError

        key = self.key("entries")
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                entry = json.loads(pipe.hget(key, entry_id))
                if entry["state"] != "active" or entry["due"] > now:
                    return None
                entry["pending"] = {
                    "id": uuid.uuid4().hex,
                    "scheduled_at": entry["due"],
                    "fired_at": now,
                    "slots": entry["slots"],
                    "delivered": [],
                }
                entry["state"] = "pending"
                pipe.multi()
                pipe.hset(key, entry_id, json.dumps(entry))
                pipe.zrem(self.key("due"), entry_id)
                pipe.sadd(self.key("pending"), entry_id)
                pipe.execute()
                return entry
            except WatchError:
                return None

    def _deliver(self, entry_id: str) -> None:
        entry = self.get(entry_id).record
        pending = entry["pending"]
        event = {
            **entry,
            "signal": entry["signal"],
            "scheduled_at": pending["scheduled_at"],
            "fired_at": pending["fired_at"],
        }
        for connection in pending["slots"]:
            if connection["id"] in pending["delivered"]:
                continue
            slot = dict(connection)
            connection_id = slot.pop("id")
            restore(slot).deliver(event, f"{pending['id']}:{connection_id}", self.redis)
            self.update(entry_id, lambda current: current["pending"]["delivered"].append(connection_id))
        self.update(entry_id, self._finish)
        self.redis.srem(self.key("pending"), entry_id)

    @staticmethod
    def _finish(entry: dict) -> None:
        entry["state"] = "fired"
        entry.pop("pending")

    def run_due(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        lock = self.redis.lock(self.key("dispatch-lock"), timeout=30, blocking=False)
        if not lock.acquire():
            raise RuntimeError("another scheduler is dispatching on this machine")
        try:
            fired = 0
            for entry_id in self.redis.zrangebyscore(self.key("due"), "-inf", now):
                if self._claim(entry_id, now) is not None:
                    fired += 1
            for entry_id in self.redis.smembers(self.key("pending")):
                if not lock.owned():
                    raise RuntimeError("scheduler lost its dispatch lock")
                entry = self.get(entry_id).record
                if "pending" in entry:
                    self._deliver(entry_id)
                else:
                    self.redis.srem(self.key("pending"), entry_id)
            return fired
        finally:
            if lock.owned():
                lock.release()
