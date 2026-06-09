import asyncio
import sqlite3
from asyncio import AbstractEventLoop
from collections.abc import Callable
from typing import Any, TypeVar

from ..protocol import ExploreItem
from .base import BaseDriver

T = TypeVar("T")


class SQLiteDriver(BaseDriver):
    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self._conn: sqlite3.Connection | None = None
        self._loop: AbstractEventLoop | None = None

    async def connect(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._conn = await self._run(
            sqlite3.connect, self.params["database"], check_same_thread=False
        )

    async def disconnect(self) -> None:
        if self._conn:
            await self._run(self._conn.close)
            self._conn = None

    async def execute(self, sql: str, binds: list[Any]) -> tuple[list[str], list[list[Any]]]:
        return await self._run(self._execute_sync, sql, binds)

    def _execute_sync(self, sql: str, binds: list[Any]) -> tuple[list[str], list[list[Any]]]:
        assert self._conn
        cur = self._conn.execute(sql, binds)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows: list[list[Any]] = [list(r) for r in cur.fetchall()]
        return columns, rows

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:
        assert self._conn
        match path:
            case []:
                rows = self._conn.execute(
                    "SELECT name, type FROM sqlite_master"
                    " WHERE type IN ('table','view') ORDER BY name"
                ).fetchall()
                return [ExploreItem(name=r[0], type=r[1], expandable=True) for r in rows]

            case [_table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indices", type="group", expandable=True),
                    ExploreItem(name="foreign_keys", type="group", expandable=True),
                ]

            case [table, "columns"]:
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                return [ExploreItem(name=r[1], type=r[2], expandable=False) for r in rows]

            case [table, "indices"]:
                rows = self._conn.execute(f"PRAGMA index_list({table})").fetchall()
                return [ExploreItem(name=r[1], type="index", expandable=False) for r in rows]

            case [table, "foreign_keys"]:
                rows = self._conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
                return [
                    ExploreItem(name=f"{r[3]} → {r[2]}.{r[4]}", type="foreign_key", expandable=False)
                    for r in rows
                ]

            case _:
                return []

    async def explore_describe(self, path: list[str]) -> dict[str, Any]:
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> dict[str, Any]:
        assert self._conn
        match path:
            case [table]:
                cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                return {
                    "table": table,
                    "columns": [
                        {"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])}
                        for r in cols
                    ],
                }
            case _:
                return {}

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        assert self._loop
        return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))
