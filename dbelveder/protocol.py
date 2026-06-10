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
    """Incoming request from the client.

    Attributes:
        id: Caller-chosen identifier echoed in the response.
        method: Method name (e.g. ``"execute"``, ``"connect"``).
        params: Method-specific parameters.
    """

    id: int
    method: str
    params: dict[str, Any]


@dataclass
class Result:
    """Final response sent to the client.

    Attributes:
        id: Matches the originating request id; None for parse errors.
        result: Return value on success; None on error.
        error: Error message on failure; None on success.
    """

    id: int | None
    result: Any
    error: str | None


@dataclass
class ProgressDetail:
    """Status update payload within a progress notification.

    Attributes:
        status: Machine-readable status key (e.g. ``"reconnecting"``).
        message: Human-readable description of the current step.
    """

    status: str
    message: str


@dataclass
class Progress:
    """Mid-request progress notification sent before the final result.

    Attributes:
        id: Matches the originating request id.
        progress: Status update payload.
    """

    id: int
    progress: ProgressDetail


@dataclass
class ExploreItem:
    """A single node in the database object tree returned by explore.list.

    Attributes:
        name: Display name of the node.
        type: Node kind (e.g. ``"table"``, ``"schema"``, ``"index"``).
        expandable: Whether the node has children that can be listed.
    """

    name: str
    type: str
    expandable: bool


@dataclass
class ColumnInfo:
    """Metadata for a single column returned by explore.describe.

    Attributes:
        name: Column name.
        type: Data type as reported by the database.
        nullable: Whether the column allows NULL; None if unknown.
        pk: Whether the column is part of the primary key.
        default: Default expression, or None if not set.
    """

    name: str
    type: str
    nullable: bool | None = None
    pk: bool = False
    default: str | None = None


@dataclass
class TableDescription:
    """Full column metadata for a table returned by explore.describe.

    Attributes:
        table: Table name.
        columns: Ordered list of column metadata.
        schema: Schema name, or None for databases without schema support.
    """

    table: str
    columns: list[ColumnInfo]
    schema: str | None = None


@dataclass
class SelectResult:
    """Result of a SELECT query.

    Attributes:
        columns: Column names in order.
        rows: Each row as a list of values.
    """

    columns: list[str]
    rows: list[list[Any]]


@dataclass
class DMLResult:
    """Result of a DML statement (INSERT, UPDATE, DELETE).

    Attributes:
        rows_affected: Number of rows inserted, updated, or deleted.
    """

    rows_affected: int


@dataclass
class DriverParam:
    """A single connection parameter announced by a driver.

    Attributes:
        key: Parameter key sent in ``connect.params``.
        type: Value type — ``"string"``, ``"integer"``, or ``"enum"``.
        label: Human-readable label for UI display.
        required: Whether a non-empty value is required.
        default: Default value pre-filled in the UI.
        choices: Allowed values for ``"enum"`` params.
        secret: Mask input in the UI; value is never persisted to disk.
    """

    key: str
    type: str
    label: str
    required: bool = False
    default: str | int | None = None
    choices: list[str] | None = None
    secret: bool = False


@dataclass
class Driver:
    """A driver and its connection parameters, as announced by ``capabilities``.

    Attributes:
        driver: Driver identifier passed as ``driver`` in ``connect.params``.
        params: Connection parameters in display order.
    """

    driver: str
    params: list[DriverParam]


Response = Result | Progress | ExploreItem


ProgressCallback = Callable[[str, str], Awaitable[None]]
"""Async callback invoked to report a status update during a long-running operation.

Args:
    status: Machine-readable status key.
    message: Human-readable description of the current step.
"""


def encode(msg: Response) -> bytes:
    """Serialize a response message to a newline-terminated JSON byte string.

    Args:
        msg: The response to encode.

    Returns:
        UTF-8 encoded JSON line ending with ``\\n``.
    """
    return (json.dumps(asdict(msg), separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> Request:
    """Parse a raw JSON line into a Request.

    Args:
        line: A single newline-terminated JSON byte string.

    Returns:
        The decoded Request.

    Raises:
        json.JSONDecodeError: If the line is not valid JSON.
        TypeError: If the JSON object is missing required fields.
    """
    return Request(**json.loads(line))
