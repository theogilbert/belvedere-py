import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any, TypeVar

from ..protocol import ColumnInfo, DMLResult, ExploreItem, SelectResult, TableDescription
from .base import BaseDriver

T = TypeVar("T")


class SQLiteDriver(BaseDriver):
    def __init__(self, params: dict[str, Any], conn: sqlite3.Connection) -> None:
        super().__init__(params)
        self._conn = conn

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "SQLiteDriver":
        loop = asyncio.get_running_loop()
        conn = await loop.run_in_executor(
            None, lambda: sqlite3.connect(params["database"], check_same_thread=False)
        )
        return cls(params, conn)

    async def reconnect(self) -> None:
        self._conn = await self._run(
            sqlite3.connect, self.params["database"], check_same_thread=False
        )

    async def disconnect(self) -> None:
        await self._run(self._conn.close)

    async def execute(self, sql: str, binds: list[Any]) -> SelectResult | DMLResult:
        return await self._run(self._execute_sync, sql, binds)

    def _execute_sync(self, sql: str, binds: list[Any]) -> SelectResult | DMLResult:

        cur = self._conn.execute(sql, binds)
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows: list[list[Any]] = [list(r) for r in cur.fetchall()]
            return SelectResult(columns=columns, rows=rows)
        return DMLResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:

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

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> TableDescription | None:

        match path:
            case [table]:
                cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                return TableDescription(
                    table=table,
                    columns=[
                        ColumnInfo(name=r[1], type=r[2], nullable=not bool(r[3]), pk=bool(r[5]))
                        for r in cols
                    ],
                )
            case _:
                return None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))
