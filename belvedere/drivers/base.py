from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from typing import Any, ClassVar, Self

from ..protocol import (
    DescribeResult,
    DriverParam,
    ExploreItem,
    Language,
    ReadResult,
    RelationshipDescription,
    TableDescription,
    TableReference,
    WriteResult,
)


def build_relationship_description(
    desc: TableDescription, table: str, schema: str | None, column: str
) -> RelationshipDescription | None:
    """Build the ``RelationshipDescription`` for one of *table*'s own outgoing
    FK columns, for a describe path ending in ``["relationships", column]``.

    Returns:
        None if *column* does not name one of *table*'s own foreign keys.
    """
    ref = next((r for r in desc.outgoing_references if r.column == column), None)
    if ref is None:
        return None
    return RelationshipDescription(
        table=table,
        schema=schema,
        column=ref.column,
        ref_table=ref.table,
        ref_schema=ref.schema,
        ref_column=ref.ref_column,
        constraint_name=ref.constraint_name,
    )


def group_references_by_column(
    refs: list[TableReference],
) -> dict[str, list[TableReference]]:
    """Group a table's outgoing FK references by local column, for populating
    ``ColumnDescription.outgoing_references``."""
    by_column: dict[str, list[TableReference]] = {}
    for ref in refs:
        by_column.setdefault(ref.column, []).append(ref)
    return by_column


@dataclass(frozen=True)
class DriverSettings:
    """Server-level configuration injected into every driver on connect."""

    column_sample_size: int = 3
    """Number of distinct non-null values sampled per column in describe results."""

    column_sample_timeout: float = 5.0
    """Seconds to wait for each column sample query before returning no sample."""


class DriverError(Exception):
    """Raised by drivers for errors that should be surfaced verbatim to the client."""


class ConnectionLostError(Exception):
    """Raised when the database connection is lost and a reconnect should be attempted."""


class BaseDriver(ABC):
    """Base class for all database drivers.

    Args:
        params: Raw connect request fields (e.g. ``{"driver": "sqlite", "database": "..."}``).
    """

    LABEL: str = ""
    """Human-readable display name declared by each driver subclass."""

    PARAMS: list[DriverParam] = []
    """Connection parameters declared by each driver subclass."""

    LANGUAGES: ClassVar[list[Language]] = []
    """Query languages this driver supports (see :class:`~belvedere.protocol.Language`).

    Drivers with no language affinity leave this empty.
    """

    HELP: str = ""
    """Markdown help text declared by each driver subclass."""

    DEFAULT_IDLE_TIMEOUT: ClassVar[float] = 600
    """The default idle timeout for this driver.

    Connections idle for longer than the specified time will be automatically closed.
    The value can be set to 0 to disable closing the connection when idle too long.
    """

    def __init__(self, params: dict[str, Any], settings: DriverSettings) -> None:
        self.params = params
        self._settings = settings

    @classmethod
    @abstractmethod
    async def create(cls, params: dict[str, Any], settings: DriverSettings) -> Self:
        """Open a new connection and return a ready-to-use driver instance.

        Args:
            params: Raw connect request fields.

        Returns:
            A fully connected driver instance.
        """
        ...

    @abstractmethod
    async def reconnect(self) -> None:
        """Re-establish the database connection on the current instance."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the database connection."""
        ...

    @abstractmethod
    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a database query and return the result.

        Args:
            query: Query to execute.
            binds: Positional bind parameters.

        Returns:
            ReadResult for queries that return rows, DMLResult for write operations.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        ...

    @abstractmethod
    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        """List child nodes at the given path in the object tree.

        Args:
            path: Ordered path segments from the root (e.g. ``["dbo", "users"]``).

        Returns:
            Child nodes, or an empty list if the path is unrecognised.
        """
        ...

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        """Return a sample of rows for the node at the given path.

        Args:
            path: Ordered path segments identifying a table or collection node.

        Returns:
            Up to 10 rows as a ReadResult, or None if the node type does not
            support row preview.
        """
        return None

    @abstractmethod
    async def explore_describe(self, path: list[str]) -> DescribeResult:
        """Return column metadata for the node at the given path.

        Contract: when a path yields a ``ColumnsDescription``, describing the
        child path ``[*path, column_name]`` must yield an equivalent
        ``ColumnDescription`` — the explore cache pre-populates per-column
        entries from the columns result relying on this.

        Args:
            path: Ordered path segments identifying a node.

        Returns:
            Column metadata, or None if the path does not resolve to a node.
        """
        ...


SAMPLE_SCAN_ROWS = 50
"""Table rows a driver scans in one query to derive per-column samples."""

_SAMPLEABLE_TYPES = (str, int, float, date, time, Decimal)
"""Sample value types the wire protocol can serialise (``date`` covers ``datetime``).
Others (LOB handles, bytes, intervals) are skipped."""


def build_column_samples(
    columns: list[str], rows: list[tuple], n: int
) -> dict[str, list[Any]]:
    """Map each column name to up to *n* distinct non-null values from *rows*.

    Values whose type is not in :data:`_SAMPLEABLE_TYPES` are skipped. Sparse
    or low-cardinality columns may yield fewer than *n* values.
    """
    samples: dict[str, list[Any]] = {name: [] for name in columns}
    for i, name in enumerate(columns):
        seen = samples[name]
        for row in rows:
            value = row[i]
            if value is None or not isinstance(value, _SAMPLEABLE_TYPES):
                continue
            if value not in seen:
                seen.append(value)
                if len(seen) == n:
                    break
    return samples
