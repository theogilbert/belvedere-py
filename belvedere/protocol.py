"""
Wire format: newline-delimited JSON (one message per line).

Request  (nvim → python): {id: int, method: str, params: dict}
Response (python → nvim): {id: int, result: any, error: str|None}
Progress (python → nvim): {id: int, progress: {status: str, message: str}}
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Request:
    """Incoming request from the client."""

    id: int
    """Caller-chosen identifier echoed in the response."""
    method: str
    """Method name (e.g. ``"execute"``, ``"connect"``)."""
    params: dict[str, Any]
    """Method-specific parameters."""


@dataclass
class Result:
    """Final response sent to the client."""

    id: int | None
    """Matches the originating request id; None for parse errors."""
    result: Any
    """Return value on success; None on error."""
    error: str | None
    """Error message on failure; None on success."""


@dataclass
class ProgressDetail:
    """Status update payload within a progress notification."""

    status: str
    """Machine-readable status key (e.g. ``"reconnecting"``)."""
    message: str
    """Human-readable description of the current step."""


@dataclass
class Progress:
    """Mid-request progress notification sent before the final result."""

    id: int
    """Matches the originating request id."""
    progress: ProgressDetail
    """Status update payload."""


@dataclass
class ExploreItem:
    """A single node in the database object tree returned by explore.list."""

    name: str
    """Display name of the node."""
    type: str
    """Node kind (e.g. ``"table"``, ``"schema"``, ``"index"``)."""
    expandable: bool
    """Whether the node has children that can be listed."""


@dataclass
class ColumnInfo:
    """Metadata for a single column returned by explore.describe."""

    name: str
    """Column name."""
    type: str
    """Data type as reported by the database."""
    nullable: bool | None = None
    """Whether the column allows NULL; None if unknown."""
    pk: bool = False
    """Whether the column is part of the primary key."""
    default: str | None = None
    """Default expression, or None if not set."""


@dataclass
class TableDescription:
    """Full column metadata for a table returned by explore.describe."""

    table: str
    """Table name."""
    columns: list[ColumnInfo]
    """Ordered list of column metadata."""
    schema: str | None = None
    """Schema name, or None for databases without schema support."""


@dataclass
class SelectResult:
    """Result of a SELECT query."""

    columns: list[str]
    """Column names in order."""
    rows: list[list[Any]]
    """Each row as a list of values."""


@dataclass
class DMLResult:
    """Result of a DML statement (INSERT, UPDATE, DELETE)."""

    rows_affected: int
    """Number of rows inserted, updated, or deleted."""


@dataclass
class DriverParam:
    """A single connection parameter announced by a driver."""

    key: str
    """Parameter key sent in ``connect.params``."""
    type: str
    """Value type — ``"string"``, ``"integer"``, or ``"enum"``."""
    label: str
    """Human-readable label for UI display."""
    required: bool = False
    """Whether a non-empty value is required."""
    default: str | int | None = None
    """Default value pre-filled in the UI."""
    choices: list[str] | None = None
    """Allowed values for ``"enum"`` params."""
    secret: bool = False
    """Mask input in the UI; value is never persisted to disk."""


@dataclass
class Driver:
    """A driver and its connection parameters, as announced by ``capabilities``."""

    driver: str
    """Driver identifier passed as ``driver`` in ``connect.params``."""
    label: str
    """Human-readable display name (e.g. ``"SQLite"``)."""
    params: list[DriverParam]
    """Connection parameters in display order."""


class Method(StrEnum):
    CAPABILITIES    = "capabilities"
    DRIVER_HELP     = "driver.help"
    CONNECT         = "connect"
    DISCONNECT      = "disconnect"
    EXECUTE         = "execute"
    EXPLORE_LIST    = "explore.list"
    EXPLORE_DESCRIBE = "explore.describe"

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
    req = Request(**json.loads(line))
    if not isinstance(req.params, dict):
        raise TypeError(
            f"Request {req.id!r}: params must be an object, got {type(req.params).__name__}"
        )
    return req
