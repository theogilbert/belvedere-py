from abc import ABC, abstractmethod
from typing import Any, Self

from ..protocol import DMLResult, ExploreItem, SelectResult, TableDescription


class ConnectionLostError(Exception):
    """Raised by a driver when the database connection has been lost and a reconnect should be attempted."""


class BaseDriver(ABC):
    """
    All drivers receive the raw `params` dict from the connect request,
    e.g. {"driver": "sqlite", "database": "/path/to/db.sqlite"}.

    explore_list(path) uses a path list to navigate the hierarchy:
      []              → top-level items  (schemas, databases, …)
      ["schema"]      → items inside schema
      ["schema","tbl"]→ items inside table (columns, indices, …)
    """

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params

    @classmethod
    @abstractmethod
    async def create(cls, params: dict[str, Any]) -> Self: ...

    @abstractmethod
    async def reconnect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def execute(
        self, sql: str, binds: list[Any]
    ) -> SelectResult | DMLResult: ...

    @abstractmethod
    async def explore_list(self, path: list[str]) -> list[ExploreItem]: ...

    @abstractmethod
    async def explore_describe(self, path: list[str]) -> TableDescription | None: ...
