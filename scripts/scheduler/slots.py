from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redis import Redis


class Slot(Protocol):
    def record(self) -> dict: ...

    def deliver(self, event: dict, delivery_id: str, redis: "Redis") -> None: ...


@dataclass(frozen=True)
class InboxSlot:
    address: str

    def record(self) -> dict:
        return {"kind": "inbox", **asdict(self)}

    def deliver(self, event: dict, delivery_id: str, redis: "Redis") -> None:
        from scripts.inbox.store import DuplicateItem, InboxStore

        try:
            InboxStore(redis).send(event["owner"], self.address, event["payload"], item_id=delivery_id)
        except DuplicateItem:
            pass


@dataclass(frozen=True)
class BroadcastSlot:
    channel: str | None = None

    def record(self) -> dict:
        return {"kind": "broadcast", **asdict(self)}

    def deliver(self, event: dict, delivery_id: str, redis: "Redis") -> None:
        from hooks.context.broadcast import create_broadcast

        create_broadcast(event["payload"], source=event["owner"], channel=self.channel, delivery_id=delivery_id)


def restore(record: dict) -> Slot:
    fields = dict(record)
    kind = fields.pop("kind")
    return {"inbox": InboxSlot, "broadcast": BroadcastSlot}[kind](**fields)
