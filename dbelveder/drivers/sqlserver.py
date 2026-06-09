"""SQL Server driver — requires: pip install mssql-python"""

import asyncio
from asyncio import AbstractEventLoop
from collections.abc import Callable
from typing import Any, TypeVar

from ..protocol import ColumnInfo, DMLResult, ExploreItem, SelectResult, TableDescription
import mssql_python

from .base import BaseDriver, ConnectionLostError

T = TypeVar("T")

_READ_ONLY_INTENT = "READ_ONLY"


class SQLServerDriver(BaseDriver):
    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self._conn: mssql_python.Connection | None = None
        self._loop: AbstractEventLoop | None = None

    async def connect(self) -> None:
        self._loop = asyncio.get_event_loop()
        host = self.params.get("host", "localhost")
        port = self.params.get("port", 1433)
        intent = self.params.get("applicationIntent", "")
        self._conn = await self._run(
            mssql_python.connect,
            server=f"{host},{port}",
            uid=self.params.get("user", ""),
            pwd=self.params.get("password", ""),
            database=self.params.get("database", ""),
            intent=intent,
            autocommit=intent == _READ_ONLY_INTENT,
            trustservercertificate="yes",
        )

    async def disconnect(self) -> None:
        if self._conn:
            await self._run(self._conn.close)
            self._conn = None

    async def execute(
        self, sql: str, binds: list[Any]
    ) -> SelectResult | DMLResult:
        try:
            return await self._run(self._execute_sync, sql, binds)
        except Exception as exc:
            if isinstance(exc, (mssql_python.OperationalError, mssql_python.InterfaceError)):
                raise ConnectionLostError(str(exc)) from exc
            raise

    def _execute_sync(
        self, sql: str, binds: list[Any]
    ) -> SelectResult | DMLResult:
        assert self._conn
        cur = self._conn.execute(sql, binds)
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows: list[list[Any]] = [list(r) for r in cur.fetchall()]
            return SelectResult(columns=columns, rows=rows)
        return DMLResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:
        assert self._conn
        cur = self._conn.cursor()
        match path:
            case []:
                cur.execute(
                    "SELECT name FROM sys.schemas"
                    " WHERE name NOT IN ('sys','INFORMATION_SCHEMA','guest','db_owner',"
                    "'db_accessadmin','db_securityadmin','db_ddladmin','db_backupoperator',"
                    "'db_datareader','db_datawriter','db_denydatareader','db_denydatawriter')"
                    " ORDER BY name"
                )
                return [
                    ExploreItem(name=r[0], type="schema", expandable=True)
                    for r in cur.fetchall()
                ]

            case [schema]:
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES"
                    " WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME",
                    (schema,),
                )
                return [
                    ExploreItem(name=r[0], type=r[1].lower(), expandable=True)
                    for r in cur.fetchall()
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indices", type="group", expandable=True),
                    ExploreItem(name="constraints", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                    " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    " ORDER BY ORDINAL_POSITION",
                    (schema, table),
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False)
                    for r in cur.fetchall()
                ]

            case [schema, table, "indices"]:
                cur.execute(
                    "SELECT i.name, i.type_desc"
                    " FROM sys.indexes i"
                    " JOIN sys.objects o ON i.object_id = o.object_id"
                    " JOIN sys.schemas s ON o.schema_id = s.schema_id"
                    " WHERE s.name = ? AND o.name = ? AND i.name IS NOT NULL"
                    " ORDER BY i.name",
                    (schema, table),
                )
                return [
                    ExploreItem(name=r[0], type=r[1].lower(), expandable=False)
                    for r in cur.fetchall()
                ]

            case [schema, table, "constraints"]:
                cur.execute(
                    "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE"
                    " FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS"
                    " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    " ORDER BY CONSTRAINT_NAME",
                    (schema, table),
                )
                return [
                    ExploreItem(
                        name=r[0],
                        type=r[1].lower().replace(" ", "_"),
                        expandable=False,
                    )
                    for r in cur.fetchall()
                ]

            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> TableDescription | None:
        assert self._conn
        match path:
            case [schema, table]:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT"
                    " FROM INFORMATION_SCHEMA.COLUMNS"
                    " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    " ORDER BY ORDINAL_POSITION",
                    (schema, table),
                )
                return TableDescription(
                    table=table,
                    schema=schema,
                    columns=[
                        ColumnInfo(name=r[0], type=r[1], nullable=r[2] == "YES", default=r[3])
                        for r in cur.fetchall()
                    ],
                )
            case _:
                return None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        assert self._loop
        return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))
