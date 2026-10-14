from dataclasses import dataclass

import orjson

from .rows import encode


@dataclass(frozen=True, slots=True)
class Fragment:
    value: object
    text: str
    parts: dict | tuple | None = None
    signature: bytes | str | None = None


def leaf(value: object, previous: Fragment | None = None) -> Fragment:
    try:
        signature = orjson.dumps(value)
    except orjson.JSONEncodeError:
        signature = encode(value)
    if previous is not None and value == previous.value and signature == previous.signature:
        return previous
    return Fragment(value, encode(value), signature=signature)


def sequence(value: list, previous: Fragment | None = None) -> Fragment:
    old = previous.parts if previous is not None and isinstance(previous.parts, tuple) else ()
    parts = tuple(leaf(item, old[index] if index < len(old) else None) for index, item in enumerate(value))
    text = "[" + ",".join(item.text for item in parts) + "]"
    if previous is not None and previous.text == text:
        return previous
    return Fragment([item.value for item in parts], text, parts)


def mapping(value: dict, previous: Fragment | None, make) -> Fragment:
    old = previous.parts if previous is not None and isinstance(previous.parts, dict) else {}
    parts = {key: make(key, item, old.get(key)) for key, item in value.items()}
    text = "{" + ",".join(encode(key) + ":" + item.text for key, item in parts.items()) + "}"
    if previous is not None and previous.text == text:
        return previous
    return Fragment({key: item.value for key, item in parts.items()}, text, parts)


def meta_part(key, value, previous):
    if key == "events" and isinstance(value, list):
        return sequence(value, previous)
    if key == "stamps" and isinstance(value, dict):
        return mapping(value, previous, lambda key, item, old: leaf(item, old))
    return leaf(value, previous)


def document_part(key, value, previous):
    if key == "_meta" and isinstance(value, dict):
        return mapping(value, previous, meta_part)
    if isinstance(value, list):
        return sequence(value, previous)
    return leaf(value, previous)


def document(state: dict, previous: Fragment | None = None) -> Fragment:
    return mapping(state, previous, document_part)
