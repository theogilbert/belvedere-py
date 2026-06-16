from abc import ABC, abstractmethod
from typing import Any, Self

from ..protocol import (
    WriteResult,
    DriverParam,
    ExploreItem,
    ReadResult,
    TableDescription,
)


class DriverError(Exception):
    """Raised by drivers for errors that should be surfaced verbatim to the client."""


class ConnectionLostError(Exception):
    """Raised when the database connection is lost and a reconnect should be attempted."""


class BaseDriver(ABC):
    """Base class for all database drivers.

    Args:
        params: Raw connect request fields (e.g. ``{"driver": "sqlite", "database": "..."}``).
    """

    PARAMS: list[DriverParam] = []
    """Connection parameters declared by each driver subclass."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params

    @classmethod
    @abstractmethod
    async def create(cls, params: dict[str, Any]) -> Self:
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

    @abstractmethod
    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        """Return column metadata for the node at the given path.

        Args:
            path: Ordered path segments identifying a node.

        Returns:
            Column metadata, or None if the path does not resolve to a node.
        """
        ...
