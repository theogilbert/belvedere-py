"""
Wire format: newline-delimited JSON (one message per line).

Request  (nvim → python): {id: int, method: str, params: dict}
Response (python → nvim): {id: int, result: any, error: str|None}
Progress (python → nvim): {id: int, progress: {status: str, message: str}}
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Request:
    id: int
    method: str
    params: dict[str, Any]


@dataclass
class Result:
    id: int | None
    result: Any
    error: str | None


@dataclass
class ProgressDetail:
    status: str
    message: str


@dataclass
class Progress:
    id: int
    progress: ProgressDetail


@dataclass
class ExploreItem:
    name: str
    type: str
    expandable: bool


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool | None = None
    pk: bool = False
    default: str | None = None


@dataclass
class TableDescription:
    table: str
    columns: list[ColumnInfo]
    schema: str | None = None


@dataclass
class SelectResult:
    columns: list[str]
    rows: list[list[Any]]


@dataclass
class DMLResult:
    # DML = Data Manipulation Language (INSERT, UPDATE, DELETE)
    rows_affected: int


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
    return (json.dumps(asdict(msg), separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> Request:
    return Request(**json.loads(line))
