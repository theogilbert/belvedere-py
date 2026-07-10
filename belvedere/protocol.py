"""
Wire format: newline-delimited JSON (one message per line).

Request  (client → server): {id: int, method: str, params: dict}
Response (server → client): {id: int, result: any, error: str|None}
Progress (server → client): {id: int, progress: {status: str, message: str}}
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class Method(StrEnum):
    """Supported request methods."""

    CAPABILITIES = "capabilities"
    DRIVER_HELP = "driver.help"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    EXECUTE = "execute"
    CANCEL = "cancel"
    EXPLORE_LIST = "explore.list"
    EXPLORE_DESCRIBE = "explore.describe"
    EXPLORE_PREVIEW = "explore.preview"
    EXPLORE_DIAGRAM = "explore.diagram"


@dataclass
class Request:
    """Incoming request from the client."""

    id: int
    """Caller-chosen identifier echoed in the response."""
    method: Method
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
    exclusive_index: bool = False
    """Whether this column is covered by an index that spans only this column."""
    composite_index: bool = False
    """Whether this column is covered by an index that also spans other columns."""


@dataclass
class TableReference:
    """One column-level leg of a foreign key relating this table to another."""

    column: str
    """Local column participating in the relationship."""
    table: str
    """Name of the other table."""
    ref_column: str
    """Column on the other table."""
    schema: str | None = None
    """Schema of the other table, or None for databases without schema support."""


@dataclass
class TableDescription:
    """Full column metadata for a table returned by explore.describe."""

    table: str
    """Table name."""
    columns: list[ColumnInfo]
    """Ordered list of column metadata."""
    schema: str | None = None
    """Schema name, or None for databases without schema support."""
    comment: str | None = None
    """Table comment as stored in the database; None if unsupported or not set."""
    outgoing_references: list[TableReference] = field(default_factory=list)
    """Foreign keys defined on this table that reference other tables in the same schema."""
    incoming_references: list[TableReference] = field(default_factory=list)
    """Foreign keys on other tables in the same schema that reference this table."""
    type: str = "table"
    """Discriminator — always ``"table"``."""


@dataclass
class IndexKeyField:
    """One field in an index key."""

    name: str
    """Field name."""
    direction: str
    """Sort direction or index kind (``"asc"``, ``"desc"``, ``"text"``, ``"hashed"``, …)."""


@dataclass
class IndexDescription:
    """Key field metadata for an index returned by explore.describe."""

    index: str
    """Index name."""
    fields: list[IndexKeyField]
    """Ordered list of key fields."""
    unique: bool = False
    """Whether the index enforces uniqueness."""
    tables: list[str] = field(default_factory=list)
    """Tables (or labels/collections) the index operates on.
    Typically one entry; multiple for Oracle cluster indexes and SQL Server indexed views."""
    index_type: str | None = None
    """Storage type as reported by the driver (e.g. ``"btree"``, ``"hash"``, ``"bitmap"``); None if unknown."""
    clustered: bool = False
    """Whether the index defines the physical row order of the table."""
    visible: bool = True
    """Whether the query optimiser considers this index; False for Oracle INVISIBLE or SQL Server DISABLED."""
    included_columns: list[str] = field(default_factory=list)
    """Non-key columns stored in index leaf pages for covering queries (PostgreSQL / SQL Server INCLUDE)."""
    ddl: str | None = None
    """CREATE INDEX statement as stored by the database; None when the driver cannot produce it."""
    type: str = "index"
    """Discriminator — always ``"index"``."""


@dataclass
class IndicesDescription:
    """All index metadata for a table returned by explore.describe on an indices group node."""

    indices: list[IndexDescription]
    """All indexes on this table, in driver-defined order."""
    type: str = "indices"
    """Discriminator — always ``"indices"``."""


@dataclass
class ColumnDescription:
    """Detailed metadata for a single column returned by explore.describe."""

    name: str
    """Column name."""
    data_type: str
    """Data type as reported by the database."""
    nullable: bool | None = None
    """Whether the column allows NULL; None if unknown."""
    pk: bool = False
    """Whether the column is part of the primary key."""
    default: str | None = None
    """Default expression, or None if not set."""
    exclusive_indices: list[IndexDescription] = field(default_factory=list)
    """Indices that cover only this column."""
    composite_indices: list[IndexDescription] = field(default_factory=list)
    """Indices that cover this column and at least one other column."""
    comment: str | None = None
    """Column comment as stored in the database; None if unsupported or not set."""
    sample: list[Any] = field(default_factory=list)
    """Up to 3 distinct non-null representative values sampled from the column."""
    type: str = "column"
    """Discriminator — always ``"column"``."""


@dataclass
class ColumnsDescription:
    """All column detail metadata for a table returned by explore.describe on a columns group node."""

    columns: list[ColumnDescription]
    """All columns in this table, in declaration order."""
    type: str = "columns"
    """Discriminator — always ``"columns"``."""


DescribeResult = (
    TableDescription
    | IndexDescription
    | IndicesDescription
    | ColumnDescription
    | ColumnsDescription
    | None
)
"""Return type of ``explore_describe`` across all drivers."""


@dataclass
class LobPlaceholder:
    """Stands in for a large object cell value a driver did not inline into a row.

    Tagging the value with an object — rather than a formatted string — lets
    clients distinguish it from a real string cell without pattern-matching
    cell contents.
    """

    text: str
    """Server-formatted placeholder text to display, e.g. ``"CLOB (3423 chars)"``."""
    type: str = "lob"
    """Discriminator — always ``"lob"``."""


@dataclass
class DiagramRegion:
    """One span in the ``diagram`` string returned by explore.diagram that names a
    table or column, letting a client resolve a cursor position to an
    explore.describe path without parsing the diagram text itself."""

    row: int
    """0-indexed line number within ``diagram`` (lines split on ``\\n``)."""
    col_start: int
    """0-indexed byte offset (not codepoints) where the span starts."""
    col_end: int
    """0-indexed byte offset where the span ends (exclusive)."""
    path: list[str]
    """Path to pass as explore.describe's ``path`` param to describe this table or column."""


@dataclass
class ReadResult:
    """Result of a read-only query."""

    columns: list[str]
    """Column names in order."""
    rows: list[list[Any]]
    """Each row as a list of values; a value may be a :class:`LobPlaceholder`."""
    rows_total: int
    """Total number of rows matching the query (may exceed len(rows) when the driver applies a default limit)."""


@dataclass
class WriteResult:
    """Result of a write query."""

    rows_affected: int
    """Number of rows inserted, updated, or deleted."""


@dataclass
class DriverParamChoice:
    """A single option within an ``"enum"`` driver parameter."""

    value: str
    """Machine-readable value sent in ``connect.params``."""
    label: str
    """Human-readable display name shown in the UI."""


class Language(StrEnum):
    """Standard query-language identifiers reported by drivers in ``capabilities``.

    These are language-neutral identifiers — mapping them to editor-specific
    concepts (e.g. Vim filetypes) is the client's responsibility.
    """

    SQL = "sql"
    CYPHER = "cypher"


class ParamType(StrEnum):
    """Represent the possible types a driver parameter can have."""

    STRING = "string"
    INTEGER = "integer"
    ENUM = "enum"


@dataclass
class DriverParam:
    """A single connection parameter announced by a driver."""

    key: str
    """Parameter key sent in ``connect.params``."""
    type: ParamType
    """The type of values accepted for this parameter."""
    label: str
    """Human-readable label for UI display."""
    required: bool = True
    """Whether a non-empty value is required."""
    default: str | int | None = None
    """Default value pre-filled in the UI."""
    choices: list[DriverParamChoice] | None = None
    """Allowed options for ``"enum"`` params."""
    secret: bool = False
    """Mask input in the UI; value is never persisted to disk by this server."""


@dataclass
class Driver:
    """A driver and its connection parameters, as announced by ``capabilities``."""

    driver: str
    """Driver identifier passed as ``driver`` in ``connect.params``."""
    label: str
    """Human-readable display name (e.g. ``"SQLite"``)."""
    params: list[DriverParam]
    """Connection parameters in display order."""
    languages: list[Language] = field(default_factory=list)
    """Query languages this driver supports, using the standard :class:`Language` enum.

    Clients map these to editor-specific concepts (e.g. Vim filetypes) and may
    use them to prioritise matching drivers in a connection picker.  An empty
    list means the driver has no language affinity and is treated as generic.
    """


Response = Result | Progress | ExploreItem


ProgressCallback = Callable[[str, str], Awaitable[None]]
"""Async callback invoked to report a status update during a long-running operation.

Args:
    status: Machine-readable status key.
    message: Human-readable description of the current step.
"""


def json_default(obj: Any) -> Any:
    """Convert database values ``json.dumps`` cannot handle natively.

    Pass as the ``default`` argument to ``json.dumps`` wherever protocol
    types (and the sample values they carry) are serialized.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def encode(msg: Response) -> bytes:
    """Serialize a response message to a newline-terminated JSON byte string.

    Args:
        msg: The response to encode.

    Returns:
        UTF-8 encoded JSON line ending with ``\\n``.
    """
    return (
        json.dumps(asdict(msg), separators=(",", ":"), default=json_default) + "\n"
    ).encode()


class DecodeError(Exception):
    """Raised when a raw line cannot be parsed into a valid Request."""

    def __init__(self, message: str, id: int | None = None) -> None:
        super().__init__(message)
        self.id = id


def decode(line: bytes) -> Request:
    """Parse a raw JSON line into a Request.

    Args:
        line: A single newline-terminated JSON byte string.

    Returns:
        The decoded Request.

    Raises:
        DecodeError: If the line is not valid JSON, is not a JSON object,
            has missing or unexpected fields, or has a non-object params value.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise DecodeError(f"Invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise DecodeError(f"Request must be a JSON object, got {type(data).__name__}")

    req_id = data.get("id")
    if not isinstance(req_id, int):
        raise DecodeError(f"Invalid request ID: {req_id!r}. Must be an integer.")

    try:
        data["method"] = Method(data.pop("method"))
    except (ValueError, KeyError) as e:
        raise DecodeError("Invalid or missing method in request", id=req_id) from e

    try:
        req = Request(**data)
    except TypeError as e:
        raise DecodeError(str(e), id=req_id) from e

    if not isinstance(req.params, dict):
        raise DecodeError(
            f"params must be a JSON object, got {type(req.params).__name__}",
            id=req.id,
        )

    return req
