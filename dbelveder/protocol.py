"""
Wire format: newline-delimited JSON (one message per line).

Request  (nvim → python): {id: int, method: str, params: dict}
Response (python → nvim): {id: int, result: any, error: str|None}
"""
import json
from typing import Any, TypedDict


class Request(TypedDict):
    id: int
    method: str
    params: dict[str, Any]


class Response(TypedDict):
    id: int | None
    result: Any
    error: str | None


class ExploreItem(TypedDict):
    name: str
    type: str
    expandable: bool


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> dict[str, Any]:
    return json.loads(line)
