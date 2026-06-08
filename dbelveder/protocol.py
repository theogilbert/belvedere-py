"""
Wire format: newline-delimited JSON (one message per line).

Request  (nvim → python): {id: int, method: str, params: dict}
Response (python → nvim): {id: int, result: any, error: str|None}
Progress (python → nvim): {id: int, progress: {status: str, message: str}}
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict


class Request(TypedDict):
    id: int
    method: str
    params: dict[str, Any]


class Result(TypedDict):
    id: int | None
    result: Any
    error: str | None


class ProgressDetail(TypedDict):
    status: str
    message: str


class Progress(TypedDict):
    id: int
    progress: ProgressDetail


class ExploreItem(TypedDict):
    name: str
    type: str
    expandable: bool


Response = Result | Progress | ExploreItem


# Async callable: send_progress(status, message)
ProgressCallback = Callable[[str, str], Awaitable[None]]
"""
Async callback to report progress on an operation to the client.

Arguments:
    status - The current operation status.
    message - A descriptive message reporting the current progress.
"""


def encode(msg: Response) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> Request:
    return json.loads(line)
