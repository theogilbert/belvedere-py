"""SQL Server driver — requires: pip install mssql-python"""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import mssql_python

from ..protocol import (
    ColumnInfo,
    DriverParam,
    DriverParamChoice,
    ExploreItem,
    Language,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from .base import BaseDriver, ConnectionLostError, DriverError

T = TypeVar("T")

_READ_ONLY_INTENT = "READ_ONLY"


class SQLServerDriver(BaseDriver):
    """SQL Server driver backed by mssql-python.

    Args:
        params: Connect request fields (``host``, ``port``, ``user``, ``password``,
            ``database``, ``applicationIntent``).
        conn: Open mssql_python connection. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "SQL Server"
    LANGUAGES = [Language.SQL]

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type=ParamType.STRING, label="Host"),
        DriverParam(key="port", type=ParamType.INTEGER, label="Port", default=1433),
        DriverParam(key="database", type=ParamType.STRING, label="Database"),
        DriverParam(key="user", type=ParamType.STRING, label="User"),
        DriverParam(
            key="applicationIntent",
            type=ParamType.ENUM,
            label="Application Intent",
            choices=[
                DriverParamChoice(value="READ_WRITE", label="READ_WRITE"),
                DriverParamChoice(value="READ_ONLY", label="READ_ONLY"),
            ],
        ),
        DriverParam(
            key="password",
            type=ParamType.STRING,
            label="Password",
            secret=True,
            required=False,
        ),
    ]

    HELP: str = """\
## SQL Server

**Install:** `pip install mssql-python`

| Parameter             | Required | Default     | Description                 |
|-----------------------|----------|-------------|------------------------------|
| `host`                | no       | `localhost` | Server hostname or IP        |
| `port`                | no       | `1433`      | TCP port                     |
| `database`            | no       | —           | Database name                |
| `user`                | no       | —           | Login name                   |
| `password`            | no       | —           | Password (masked)            |
| `applicationIntent`   | no       | —           | `READ_WRITE` or `READ_ONLY`  |

**Queries:** Standard T-SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM dbo.orders WHERE status = ?
```

**Explore tree:**

```
(root)
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        ├── indices      → name, type (e.g. CLUSTERED)
        └── constraints  → name, type (e.g. primary_key, foreign_key)
```

System schemas (`sys`, `INFORMATION_SCHEMA`, `guest`, `db_*`) are hidden.

`explore.describe` is supported on `[schema, table]` paths and returns full
column metadata (name, type, nullability, default).
"""

    def __init__(self, params: dict[str, Any], conn: "mssql_python.Connection") -> None:
        super().__init__(params)
        self._conn = conn

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "SQLServerDriver":
        """Open a SQL Server connection and return a ready-to-use driver.

        Args:
            params: Connect request fields; see class docstring for supported keys.

        Returns:
            A connected SQLServerDriver instance.
        """
        return cls(params, await cls._open(params))

    async def reconnect(self) -> None:
        self._conn = await self._open(self.params)

    async def disconnect(self) -> None:
        await self._run(self._conn.close)

    @staticmethod
    async def _open(params: dict[str, Any]) -> mssql_python.Connection:
        intent = params.get("applicationIntent", "")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda: mssql_python.connect(
                    server=f"{params.get('host', 'localhost')},{params.get('port', 1433)}",
                    uid=params.get("user", ""),
                    pwd=params.get("password", ""),
                    database=params.get("database", ""),
                    intent=intent,
                    autocommit=intent == _READ_ONLY_INTENT,
                    trustservercertificate="yes",
                ),
            )
        except Exception as exc:
            raise DriverError(str(exc)) from exc

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a SQL statement.

        Args:
            query: SQL statement to execute.
            binds: Positional bind parameters (``?`` placeholders).

        Returns:
            ReadResult for queries that return rows, DMLResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        try:
            return await self._run(self._execute_sync, query, binds)
        except Exception as exc:
            if isinstance(
                exc, (mssql_python.OperationalError, mssql_python.InterfaceError)
            ):
                raise ConnectionLostError(str(exc)) from exc
            raise DriverError(str(exc)) from exc

    def _execute_sync(self, sql: str, binds: list[Any]) -> ReadResult | WriteResult:

        cur = self._conn.execute(sql, binds)
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows: list[list[Any]] = [list(r) for r in cur.fetchall()]  # ty: ignore[missing-argument]
            return ReadResult(columns=columns, rows=rows, rows_total=len(rows))
        return WriteResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        """List child nodes at the given path in the SQL Server object tree.

        Args:
            path: Path segments (``[]`` for schemas, ``[schema]`` for tables,
                ``[schema, table]`` for groups, etc.).

        Returns:
            Child nodes, or an empty list if the path is unrecognised.
        """
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:

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
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case [schema]:
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES"
                    " WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME",
                    (schema,),
                )
                return [
                    ExploreItem(name=r[0], type=r[1].lower(), expandable=True)
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
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
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
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
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
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
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [schema, table]:
                result = await self.execute(
                    f"SELECT TOP 10 * FROM [{schema}].[{table}]", []
                )
                return result if isinstance(result, ReadResult) else None
            case _:
                return None

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        """Return column metadata for the table at the given path.

        Args:
            path: Two-element path with schema and table name (e.g. ``["dbo", "users"]``).

        Returns:
            TableDescription if the path resolves to a table, None otherwise.
        """
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> TableDescription | None:

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
                col_rows = cur.fetchall()  # ty: ignore[missing-argument]
                cur.execute(
                    "SELECT c.name, i.name"
                    " FROM sys.indexes i"
                    " JOIN sys.index_columns ic"
                    "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id"
                    " JOIN sys.columns c"
                    "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id"
                    " JOIN sys.objects o ON i.object_id = o.object_id"
                    " JOIN sys.schemas s ON o.schema_id = s.schema_id"
                    " WHERE s.name = ? AND o.name = ? AND i.name IS NOT NULL",
                    (schema, table),
                )
                col_indexes: dict[str, list[str]] = {}
                for col_name, idx_name in cur.fetchall():  # ty: ignore[missing-argument]
                    col_indexes.setdefault(col_name, []).append(idx_name)
                return TableDescription(
                    table=table,
                    schema=schema,
                    columns=[
                        ColumnInfo(
                            name=r[0],
                            type=r[1],
                            nullable=r[2] == "YES",
                            default=r[3],
                            indexes=col_indexes.get(r[0], []),
                        )
                        for r in col_rows
                    ],
                )
            case _:
                return None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: fn(*args, **kwargs)
        )
